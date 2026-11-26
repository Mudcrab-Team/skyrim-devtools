#!/usr/bin/env python3.13
"""Live check of the profiler's live view and timeline (SPDX-License-Identifier: GPL-3.0-or-later).

    skyrim-devtools-livetest run REQUEST.yaml          as a harness module: async run(ctx) on the shared session
    python3.13 tests/live/profiler_ui.py --config host.json [--case top,topload,timeline] [--seconds 20]
    python3.13 tests/live/profiler_ui.py --dry-run      every case against a fake plugin, no game

top:      idle in Riverwood; frame times without, then with a Profile.top snapshot every second.
topload:  snapshots every second while cell loads run (modules load and unload under the resolver).
timeline: a threads=all profile across cell loads with a client marker; checks the marks and timeline.html.
Prints {"pass": bool, "details": {case: {...}}}; each case lists the checks it failed.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiler  # noqa: E402
from profiler import INTERIOR, frame_window, home, wait_playable  # noqa: E402
from skyrim_devtools import profile_views, profiling  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402


async def snapshots(manager, symbols, seconds, phase):
    """Snapshots once a second for `seconds` under `phase`; returns (snapshots, latencies ms, errors, frame stats)."""
    await manager.call("Perf.reset")
    load = asyncio.create_task(profiling.drive(manager, phase)) if phase != "idle" else None
    shots, latencies, errors = [], [], []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        began = time.monotonic()
        try:
            shots.append(await profile_views.top_snapshot(manager.call, symbols, seconds=3.0))
            latencies.append(round((time.monotonic() - began) * 1000.0, 1))
        except Exception as error:  # noqa: BLE001 - a snapshot can time out during a load; counted below
            errors.append(f"{type(error).__name__}: {error}"[:200])
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - began)))
    if load:
        load.cancel()
    stats = await manager.call("Perf.frameStats", window=seconds)
    return shots, latencies, errors, {k: stats.get(k) for k in ("fps", "p50Ms", "p99Ms", "maxMs", "count")}


def slowest_cost(shots, latencies):
    """Where the slowest snapshot spent its time (native parts, RPC round trip, resolver rebuild)."""
    if not shots or len(shots) != len(latencies):
        return None
    index = max(range(len(latencies)), key=latencies.__getitem__)
    return {"latencyMs": latencies[index], **(shots[index].get("costMs") or {})}


def shot_summary(snap):
    return {"busyPct": snap["main"]["busyPct"], "samples": snap["main"]["samples"], "namedPct": snap["main"]["namedPct"],
            "top": [f"{r['pct']}% {r['function']} [{r['module']}]" for r in snap["topSelf"][:10]],
            "memory": snap.get("memory"), "gpu": snap.get("gpu"),
            "threads": (snap.get("threads") or {}).get("top", [])[:5],
            "processCpuPct": (snap.get("threads") or {}).get("processCpuPct")}


async def top_case(manager, symbols, seconds, out):
    await home(manager)
    off = await frame_window(manager, "idle", seconds)
    await home(manager)
    shots, latencies, errors, on = await snapshots(manager, symbols, seconds, "idle")
    fps_ratio = round(on["fps"] / off["fps"], 3) if on.get("fps") and off.get("fps") else None
    late = shots[3:]
    failed = []
    if errors:
        failed.append(f"{len(errors)} snapshot errors")
    if len(shots) < seconds * 0.8:
        failed.append("fewer than 0.8 snapshots per second")
    if not late or not all(s["ready"] and s["main"]["samples"] > 0 and s["topSelf"] for s in late):
        failed.append("a warm snapshot without main-thread samples or top functions")
    if not late or not all((s.get("memory") or {}).get("workingSetMb") and (s.get("memory") or {}).get("pageFaultsPerS") is not None for s in late):
        failed.append("memory or page-fault rate missing")
    if not late or not all((s.get("threads") or {}).get("top") and s["threads"].get("processCpuPct") is not None for s in late):
        failed.append("per-thread CPU missing")
    if latencies and max(latencies) > 1000:
        failed.append("a snapshot took over 1 s")
    if fps_ratio is not None and fps_ratio < 0.97 and off["fps"] < 58:
        failed.append("fps dropped more than 3% (not vsync-capped)")
    out.mkdir(parents=True, exist_ok=True)
    (out / "top-snapshots.json").write_text(json.dumps(shots, indent=1))
    (out / "top-last.txt").write_text(profile_views.render_top(shots[-1]) if shots else "")
    return {"pass": not failed, "failed": failed, "off": off, "on": on, "fpsRatio": fps_ratio, "snapshots": len(shots),
            "latencyMs": {"p50": statistics.median(latencies) if latencies else None, "max": max(latencies) if latencies else None},
            "slowest": slowest_cost(shots, latencies),
            "gpuAvailable": any((s.get("gpu") or {}).get("utilPct") is not None for s in shots),
            "last": shot_summary(shots[-1]) if shots else None, "errors": errors[:3],
            "file": str(out / "top-snapshots.json")}


async def topload_case(manager, symbols, seconds, out):
    await home(manager)
    shots, latencies, errors, on = await snapshots(manager, symbols, seconds, INTERIOR)
    failed = []
    if len(errors) > max(2, len(shots) // 4):
        failed.append(f"{len(errors)} snapshot errors during loads")
    if not any(s["topSelf"] for s in shots):
        failed.append("no snapshot with top functions")
    out.mkdir(parents=True, exist_ok=True)
    (out / "topload-snapshots.json").write_text(json.dumps(shots, indent=1))
    return {"pass": not failed, "failed": failed, "on": on, "snapshots": len(shots),
            "latencyMs": {"p50": statistics.median(latencies) if latencies else None, "max": max(latencies) if latencies else None},
            "slowest": slowest_cost(shots, latencies),
            "maxPageFaultsPerS": max(((s.get("memory") or {}).get("pageFaultsPerS") or 0) for s in shots) if shots else None,
            "minResidentPct": min(((s.get("memory") or {}).get("residentPct") or 100) for s in shots) if shots else None,
            "errors": errors[:3]}


async def timeline_case(manager, symbols, seconds, out):
    await home(manager)
    await manager.call("Profile.start", threads="all", hz=1000, durationS=seconds + 60)
    window = await frame_window(manager, INTERIOR, seconds)
    marked = await manager.call("Profile.mark", kind="alert", name="live test marker", durationMs=50)
    raw, head = await profiling.fetch(manager.call)
    profile = profiling.Profile(raw, symbols)
    paths, _ = profiling.save_outputs(profile, out / "timeline", title="Skyrim SE timeline case")
    marks = raw.get("marks", [])
    kinds = {}
    for mark in marks:
        kinds[mark[2]] = kinds.get(mark[2], 0) + 1
    page = Path(paths["timeline"])
    failed = []
    if not marked.get("recorded"):
        failed.append("Profile.mark did not record during a profile")
    if not kinds.get("load"):
        failed.append("no load (Loading Menu) span")
    if not kinds.get("rpc"):
        failed.append("no rpc span")
    if kinds.get("alert") != 1:
        failed.append("client marker missing")
    if not raw.get("frameMarks"):
        failed.append("no frame marks")
    if len(raw.get("threads", [])) < 3:
        failed.append("fewer than 3 threads")
    if not page.exists() or page.stat().st_size > 40 * 1024 * 1024:
        failed.append("timeline.html missing or over 40 MB")
    loads = [round(m[1] - m[0], 1) for m in marks if m[2] == "load"]
    return {"pass": not failed, "failed": failed, "window": window, "marks": kinds, "loadMs": loads[:6],
            "frames": len(raw.get("frameMarks", [])), "threads": len(raw.get("threads", [])), "bytes": head.get("bytes"),
            "timelineBytes": page.stat().st_size if page.exists() else None, "files": paths}


CASES = {"top": top_case, "topload": topload_case, "timeline": timeline_case}


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
    return Path(explicit) if explicit else profiling.default_out("live-ui-" + time.strftime("%Y%m%d-%H%M%S"))


async def run(ctx):
    """Harness entry: the shared session already has the request's save loaded."""
    config = json.loads(Path(ctx.config_path).expanduser().read_text()) if ctx.config_path else {}
    symbols = profiling.GameSymbols(profiling.research_root(None, config))
    names = os.environ.get("SKYRIM_PROFILER_CASES", "top,topload,timeline").split(",")
    await wait_playable(ctx.manager)
    return await run_cases(ctx.manager, symbols, names, float(os.environ.get("SKYRIM_PROFILER_SECONDS", 20)),
                           stamp_out(os.environ.get("SKYRIM_PROFILER_OUT")))


class FakeManager(profiler.FakeManager):
    """The profiler fake plus Profile.top, Profile.mark and the marks a cell load leaves."""

    def __init__(self):
        super().__init__()
        self.profile["marks"] = [[0.0, 400.0, "load", "Loading Menu"], [1.0, 2.0, "rpc", "Perf.reset"],
                                 [500.0, 550.0, "alert", "live test marker"]]
        frame = dict(self.profile["frames"][0])
        self.top = {"seconds": 3.0, "hz": 250.0, "ready": True, "warm": True, "samples": 750, "waiting": 100,
                    "self": [dict(frame, n=650)], "total": [dict(frame, n=650)], "waits": [],
                    "modules": self.profile["modules"],
                    "memory": {"workingSetMb": 3000.0, "privateMb": 3500.0, "residentPct": 85.0, "pageFaultsPerS": 10.0},
                    "threads": {"count": 80, "processCpuPct": 120.0, "cores": 16,
                                "top": [{"tid": 1, "name": "main", "cpuS": 1.0, "pct": 90.0}]},
                    "gpu": None, "profiling": False,
                    "costMs": {"samples": 2.0, "memory": 0.5, "threads": 1.0, "total": 3.5}, "resolveMs": 1.0}

    async def call(self, method, timeout=None, **params):
        if method == "Profile.top":
            return self.top
        if method == "Profile.mark":
            return {"recorded": True}
        return await super().call(method, timeout=timeout, **params)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SKYRIM_LIVETEST_CONFIG"))
    parser.add_argument("--case", default="top,topload,timeline")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-ui-<stamp>")
    parser.add_argument("--dry-run", action="store_true", help="run every case against a fake plugin, no game")
    options = parser.parse_args()
    names = options.case.split(",")
    if options.dry_run:
        global home  # noqa: PLW0603 - the dry run skips the cell changes
        home = profiler.no_home
        import tempfile
        # top only judges snapshots after the third, so the window must hold a few
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_cases(FakeManager(), profiling.GameSymbols(), names, 5.0, Path(tmp))
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
