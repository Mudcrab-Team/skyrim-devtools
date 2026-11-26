#!/usr/bin/env python3.13
"""Live check of GPU timing, the SQLite export and NVTX marks (SPDX-License-Identifier: GPL-3.0-or-later).

    skyrim-devtools-livetest run REQUEST.yaml          as a harness module: async run(ctx) on the shared session
    python3.13 tests/live/profiler_gpu.py --config host.json [--case gputime,sqlite,top] [--seconds 20]
    python3.13 tests/live/profiler_gpu.py --dry-run     every case against a fake plugin, no game

One visible game session in Riverwood. Prints {"pass": bool, "details": {case: {...}}}; each case lists
the checks it failed. Outputs go under --out (default .local/profiles/live-gpu-<stamp>).
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiler  # noqa: E402
from profiler import INTERIOR, frame_window, home, wait_playable  # noqa: E402
import profiler_ui  # noqa: E402
from skyrim_devtools import profiling  # noqa: E402
from skyrim_devtools.profile_sqlite import query  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402


def doc_queries():
    doc = (ROOT / "docs" / "profiling.md").read_text()
    return [q.strip().rstrip(";") for q in re.findall(r"```sql\n(.*?)```", doc, re.S)]


async def profiled(manager, symbols, name, phase, threads, seconds, out):
    await home(manager)
    off = await frame_window(manager, phase, seconds)
    await home(manager)
    await manager.call("Profile.start", threads=threads, hz=1000, durationS=seconds + 60)
    marked = await manager.call("Profile.mark", name="gpu live check", kind="marker")
    on = await frame_window(manager, phase, seconds)
    raw, _ = await profiling.fetch(manager.call)
    raw["context"] = {"phase": phase, "off": off, "on": on}
    profile = profiling.Profile(raw, symbols)
    paths, summary = profiling.save_outputs(profile, out / name, title=f"Skyrim SE {name}")
    fps_ratio = round(on["fps"] / off["fps"], 3) if on.get("fps") and off.get("fps") else None
    return raw, paths, summary, off, on, fps_ratio, marked


async def gputime_case(manager, symbols, seconds, out):
    raw, paths, summary, off, on, fps_ratio, marked = await profiled(manager, symbols, "gputime", "walk", "main", seconds, out)
    gpu = summary.get("gpu") or {}
    labels = {p["pass"] for p in gpu.get("passes", [])} - {"frame", "present"}
    failed = []
    if not gpu.get("available"):
        failed.append(f"GPU timing unavailable: {gpu.get('reason')}")
    if (gpu.get("timedPct") or 0) < 80:
        failed.append("fewer than 80% of presented frames timed")
    if not (gpu.get("gpuMsP50") or 0) > 0:
        failed.append("gpu p50 is not positive")
    if len(labels) < 2:
        failed.append("fewer than 2 pass labels")
    if (gpu.get("hookUsPerFrame") or 0) > 300:
        failed.append("GPU timing costs over 300 us per frame on the CPU")
    if fps_ratio is not None and fps_ratio < 0.97 and off["fps"] < 58:
        failed.append("fps dropped more than 3% (not vsync-capped)")
    if (summary["overhead"].get("mainSuspendedPct") or 100) >= 3.0:
        failed.append("main thread suspended >= 3% of wall time")
    if "nvtx" not in marked:
        failed.append("Profile.mark reply has no nvtx field")
    if not Path(paths.get("sqlite", "")).is_file():
        failed.append("profile.sqlite not written")
    return {"pass": not failed, "failed": failed, "off": off, "on": on, "fpsRatio": fps_ratio,
            "overhead": summary["overhead"], "nvtx": marked.get("nvtx"),
            "gpu": {k: gpu.get(k) for k in ("available", "reason", "frames", "presented", "timedPct", "gpuMsP50",
                                             "gpuMsP95", "gpuMsMax", "cpuMsP50", "frameMsP50", "boundPct",
                                             "hookUsPerFrame", "disjointFrames", "staleFrames", "alignmentOffsetMs")},
            "passes": gpu.get("passes", [])[:8], "files": paths}


async def sqlite_case(manager, symbols, seconds, out):
    raw, paths, summary, off, on, fps_ratio, _ = await profiled(manager, symbols, "sqlite", INTERIOR, "main", seconds, out)
    db = Path(paths["sqlite"])
    failed, results = [], {}
    for name, sql in zip(("hitchTop", "gpuOverCpu", "cellLoads"), doc_queries()):
        try:
            began = time.monotonic()
            r = query(db, sql, limit=10)
            results[name] = {"rows": len(r["rows"]), "first": r["rows"][:3], "ms": round((time.monotonic() - began) * 1000)}
        except Exception as error:  # noqa: BLE001 - reported as a failed check
            failed.append(f"doc query {name}: {error}")
    if results.get("cellLoads", {}).get("rows", 0) == 0:
        failed.append("no samples inside cell-load markers")
    frames = query(db, "SELECT COUNT(*), COUNT(gpu_ms), SUM(hitch) FROM Frames")["rows"][0]
    counts = {t: query(db, f"SELECT COUNT(*) FROM {t}")["rows"][0][0]
              for t in ("CpuSamples", "Callchains", "Symbols", "Markers", "GpuRanges", "Counters", "ThreadNames")}
    if frames[0] == 0 or frames[1] == 0:
        failed.append("Frames has no rows with gpu_ms")
    if any(counts[t] == 0 for t in ("CpuSamples", "Callchains", "Symbols", "Markers", "GpuRanges", "Counters")):
        failed.append("an empty table")
    try:
        query(db, "DELETE FROM Frames")
        failed.append("a write was allowed")
    except sqlite3.Error:
        pass
    credits = dict(query(db, "SELECT key, value FROM Meta WHERE key = 'credits'")["rows"]).get("credits", "")
    if any("crashlogger" in s for s in symbols.loaded) and "CRASHLOGGER_PDB" not in credits:
        failed.append("Meta credits lack the CrashLogger credit")
    if summary.get("hitches") and results.get("hitchTop", {}).get("rows", 0) == 0:
        failed.append("hitches present but the hitch query returned nothing")
    return {"pass": not failed, "failed": failed, "fpsRatio": fps_ratio, "frames": frames, "counts": counts,
            "queries": results, "sqliteBytes": db.stat().st_size, "hitches": len(summary.get("hitches", []))}


async def top_case(manager, seconds):
    await home(manager)
    snaps, errors = [], []
    end = time.monotonic() + min(seconds, 10)
    while time.monotonic() < end:
        try:
            snaps.append((await manager.call("Profile.top", seconds=2)).get("gpuTiming") or {})
        except Exception as error:  # noqa: BLE001 - counted
            errors.append(str(error)[:120])
        await asyncio.sleep(1)
    last = snaps[-1] if snaps else {}
    failed = []
    if errors:
        failed.append(f"{len(errors)} snapshot errors")
    if not last.get("available") or not (last.get("frames") or 0) > 0 or not (last.get("gpuMsP50") or 0) > 0:
        failed.append("last snapshot has no GPU frame timing")
    return {"pass": not failed, "failed": failed, "snapshots": len(snaps), "last": last, "errors": errors[:3]}


CASES = {"gputime": gputime_case, "sqlite": sqlite_case, "top": lambda manager, symbols, seconds, out: top_case(manager, seconds)}


async def run_cases(manager, symbols, names, seconds, out):
    details = {"symbols": symbols.loaded, "out": str(out)}
    for name in names:
        try:
            details[name] = await CASES[name](manager, symbols, seconds, out)
        except Exception as error:  # noqa: BLE001 - one broken case must not hide the others
            details[name] = {"pass": False, "failed": [f"{type(error).__name__}: {error}"[:300]]}
            try:
                await manager.call("Profile.stop")
            except Exception:  # noqa: BLE001 - nothing was running
                pass
    ran = [details[n] for n in names if n in details]
    return {"pass": bool(ran) and all(c.get("pass") for c in ran), "details": details}


def stamp_out(explicit=None):
    return Path(explicit) if explicit else profiling.default_out("live-gpu-" + time.strftime("%Y%m%d-%H%M%S"))


async def run(ctx):
    """Harness entry: the shared session already has the request's save loaded."""
    config = json.loads(Path(ctx.config_path).expanduser().read_text()) if ctx.config_path else {}
    symbols = profiling.GameSymbols(profiling.research_root(None, config))
    names = os.environ.get("SKYRIM_PROFILER_CASES", "gputime,sqlite,top").split(",")
    await wait_playable(ctx.manager)
    return await run_cases(ctx.manager, symbols, names, float(os.environ.get("SKYRIM_PROFILER_SECONDS", 20)),
                           stamp_out(os.environ.get("SKYRIM_PROFILER_OUT")))


class FakeManager(profiler_ui.FakeManager):
    """The live-view fake plus a GPU timing block, counter series and NVTX in Profile.mark."""

    def __init__(self):
        super().__init__()
        # GPU clock reads 1000 s behind the CPU clock, as the driver's timestamp base is unrelated to QPC
        self.profile["gpu"] = {"available": True, "reason": "", "backbuffer": [1920, 1080],
                               "labels": ["frame", "present", "dsv 2048x2048 array", "rtv 1920x1080", "rtv 960x540"],
                               "disjointFrames": 0, "staleFrames": 0,
                               "frames": [[1, 0.0, 15.0, 4.0, 0, [[0, -999.9995], [2, -999.998], [3, -999.994],
                                                                   [4, -999.990], [1, -999.9845]]],
                                          [2, 16.0, 149.0, 4.0, 0, [[0, -999.8508], [3, -999.8506], [1, -999.8505]]]]}
        self.profile["frameMarks"] = [[0.0, 16.0, 1], [16.0, 150.0, 2]]
        for thread in self.profile["threads"]:
            thread["runs"].append([30.0, 0, 50, 0])  # on-CPU inside the hitch frame, for the hitch query
        self.profile["counters"] = [[0.0, 100, 2 << 30, 0, 0, 0, 8 << 30], [250.0, 150, 2 << 30, 1 << 20, 0, 1_250_000, 8 << 30]]
        self.profile["gpuCounters"] = [[500.0, 70.0, 2048.0]]
        self.top["gpuTiming"] = {"available": True, "reason": "", "frames": 120, "gpuMsP50": 9.5, "gpuMsP95": 11.0,
                                 "gpuMsMax": 14.0, "hookUsPerFrame": 20.0, "passes": []}

    async def call(self, method, timeout=None, **params):
        if method == "Profile.mark":
            return {"recorded": True, "nvtx": False}
        return await super().call(method, timeout=timeout, **params)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SKYRIM_LIVETEST_CONFIG"))
    parser.add_argument("--case", default="gputime,sqlite,top")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-gpu-<stamp>")
    parser.add_argument("--dry-run", action="store_true", help="run every case against a fake plugin, no game")
    options = parser.parse_args()
    names = options.case.split(",")
    if options.dry_run:
        global home  # noqa: PLW0603 - the dry run skips the cell changes
        home = profiler.no_home
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_cases(FakeManager(), profiling.GameSymbols(), names, 1.0, Path(tmp))
        print(json.dumps(result, default=str))
        return 0 if result["pass"] else 1
    if not options.config:
        parser.error("--config is required (or SKYRIM_LIVETEST_CONFIG)")
    config = json.loads(Path(options.config).expanduser().read_text())
    symbols = profiling.GameSymbols(profiling.research_root(options.symbols, config))
    manager = GameManager(config)
    try:
        began = time.monotonic()
        await manager.start(headless=False, save=options.save)
        await wait_playable(manager)
        started = round(time.monotonic() - began, 1)
        result = await run_cases(manager, symbols, names, options.seconds, stamp_out(options.out))
        result["details"]["startS"] = started
    finally:
        await manager.stop()
    # one line, so a harness that reads the last stdout line gets the whole verdict
    print(json.dumps(result, default=str))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
