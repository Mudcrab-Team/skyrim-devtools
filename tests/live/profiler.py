#!/usr/bin/env python3.13
"""Live check of the sampling profiler (SPDX-License-Identifier: GPL-3.0-or-later).

    skyrim-devtools-livetest run REQUEST.yaml          as a harness module: async run(ctx) on the shared session
    python3.13 tests/live/profiler.py --config host.json [--case idle,walk,cellload,watch] [--seconds 20]
    python3.13 tests/live/profiler.py --dry-run         every case against a fake plugin, no game

All cases share one game session in Riverwood. Each profiling case measures frame times for `seconds` without the
profiler, then the same load with it, fetches and symbolises the profile and writes the outputs under --out.
Prints {"pass": bool, "details": {case: {...}}}; each case lists the checks it failed.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools import profiling  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402

INTERIOR = "coc:RiverwoodSleepingGiantInn,Riverwood"
CASES = {
    "idle": {"phase": "idle", "threads": "main"},
    "walk": {"phase": "walk", "threads": "all"},
    "cellload": {"phase": INTERIOR, "threads": "all"},
}


async def frame_window(manager, phase, seconds):
    await manager.call("Perf.reset")
    load = asyncio.create_task(profiling.drive(manager, phase)) if phase != "idle" else None
    await asyncio.sleep(seconds)
    if load:
        load.cancel()
    stats = await manager.call("Perf.frameStats", window=seconds)
    return {k: stats.get(k) for k in ("fps", "meanMs", "p50Ms", "p99Ms", "maxMs", "count")}


async def wait_playable(manager, timeout=90.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            state = await manager.call("Events.snapshot")
            menus = [m.get("name") for m in state.get("menus", [])]
            if state.get("playable") and "Fader Menu" not in menus:
                return True
        except Exception:  # noqa: BLE001 - the link can stall while a cell loads
            pass
        await asyncio.sleep(0.5)
    return False


async def home(manager):
    try:
        await manager.call("Console.execute", command="coc Riverwood", timeout=60)
    except Exception:  # noqa: BLE001 - the reply can time out while the cell loads
        pass
    await wait_playable(manager)
    await asyncio.sleep(3)


async def profile_case(manager, symbols, name, spec, seconds, out):
    await home(manager)
    off = await frame_window(manager, spec["phase"], seconds)
    await home(manager)
    await manager.call("Profile.start", threads=spec["threads"], hz=1000, durationS=seconds + 60)
    on = await frame_window(manager, spec["phase"], seconds)
    began = time.monotonic()
    raw, fetched = await profiling.fetch(manager.call)
    fetch_s = time.monotonic() - began
    raw["context"] = {"phase": spec["phase"], "off": off, "on": on}
    began = time.monotonic()
    profile = profiling.Profile(raw, symbols)
    paths, summary = profiling.save_outputs(profile, out / name, title=f"Skyrim SE {name}")
    analyse_s = time.monotonic() - began
    main = summary["main"]
    overhead = summary["overhead"]
    fps_ratio = round(on["fps"] / off["fps"], 3) if on.get("fps") and off.get("fps") else None
    failed = []
    if main["onCpuSamples"] + main["waitingSamples"] < 0.5 * seconds * 1000:
        failed.append("fewer main-thread samples than half the ticks")
    if not main["topSelf"]:
        failed.append("no top-self functions")
    if summary["coverage"]["gameSamples"] == 0:
        failed.append("no game-module samples")
    if (overhead.get("mainSuspendedPct") or 100) >= 3.0:
        failed.append("main thread suspended >= 3% of wall time")
    if fps_ratio is not None and fps_ratio < 0.97 and off["fps"] < 58:
        failed.append("fps dropped more than 3% (not vsync-capped)")
    if fetch_s > 60:
        failed.append("Profile.stop + read took over 60 s")
    if not all(Path(p).exists() for p in paths.values()):
        failed.append("missing output files")
    if spec["threads"] == "all" and len(raw.get("threads", [])) < 3:
        failed.append("threads=all sampled fewer than 3 threads")
    if (overhead.get("otherMaxSuspendedPct") or 0) >= 3.0:
        failed.append("another thread suspended >= 3% of wall time")
    from skyrim_devtools import profile_firefox

    firefox_problems = profile_firefox.check(profiling.profile_firefox_data(profile))
    if firefox_problems:
        failed.append(f"Firefox export invalid: {firefox_problems[:3]}")
    garbage = [r["function"] for r in main["topSelf"] if r["module"] != "SkyrimSE.exe" and "::" in r["function"]
               and not r["function"].startswith("[")]
    if garbage:
        failed.append(f"non-game top-self names that look borrowed from distant exports: {garbage[:3]}")
    return {
        "pass": not failed, "failed": failed, "off": off, "on": on, "fpsRatio": fps_ratio,
        "overhead": overhead, "otherThreads": [t.get("name") or t["tid"] for t in raw.get("threads", []) if not t.get("main")][:12],
        "coverage": summary["coverage"], "symbolSources": summary["symbolSources"],
        "threads": len(raw.get("threads", [])), "transfer": fetched.get("transfer"),
        "fetchS": round(fetch_s, 1), "analyseS": round(analyse_s, 1), "modules": main["modules"],
        "topSelf": [f"{r['pct']}% {r['function']} [{r['module']}]" for r in main["topSelf"][:10]],
        "topWaits": [f"{r['pct']}% {r['function']}" for r in main["topWaits"][:3]],
        "hitches": len(summary["hitches"]), "files": paths,
    }


async def watch_case(manager, symbols, seconds, out):
    await home(manager)
    frame = (await manager.call("Perf.frameStats")).get("frame", 0)
    await manager.call("Profile.watch", enabled=True, hitchMs=100)
    window = await frame_window(manager, INTERIOR, seconds)
    raw = await manager.call("Profile.hitches", sinceFrame=frame, limit=20)
    perf = await manager.call("Perf.hitches", sinceFrame=frame, limit=50)
    await manager.call("Profile.watch", enabled=False)
    profile = profiling.Profile({"modules": raw["modules"], "hitches": raw["hitches"], "hitchFrames": raw["frames"],
                                 "frames": [], "stacks": []}, symbols)
    hitches = profile.hitches()
    (out / "watch-hitches.json").parent.mkdir(parents=True, exist_ok=True)
    (out / "watch-hitches.json").write_text(json.dumps({"raw": raw, "report": hitches}, indent=1))
    perf_count = len(perf.get("hitches", []))
    failed = []
    if perf_count and not hitches:
        failed.append("Perf.hitches saw slow frames but Profile.hitches kept none")
    if any(h["samples"] == 0 for h in hitches):
        failed.append("a hitch with no samples")
    if hitches and not any(h["topSelf"] or h["topWaits"] for h in hitches):
        failed.append("hitches without functions")
    return {
        "pass": not failed, "failed": failed, "window": window, "perfHitches": perf_count, "kept": len(hitches),
        "sample": [{"frame": h["frame"], "ms": h["ms"], "samples": h["samples"], "waitingPct": h["waitingPct"],
                    "top": [r["function"] for r in h["topSelf"][:3]], "waits": [r["function"] for r in h["topWaits"][:2]],
                    "waitChain": bool(h["waitChain"])} for h in hitches[:5]],
        "file": str(out / "watch-hitches.json"),
    }


async def run_cases(manager, symbols, cases, seconds, out):
    details = {"symbols": symbols.loaded, "out": str(out)}
    for name in cases:
        try:
            if name == "watch":
                details[name] = await watch_case(manager, symbols, seconds, out)
            else:
                details[name] = await profile_case(manager, symbols, name, CASES[name], seconds, out)
        except Exception as error:  # noqa: BLE001 - one broken case must not hide the others
            details[name] = {"pass": False, "failed": [f"{type(error).__name__}: {error}"[:300]]}
            import traceback
            details[name]["trace"] = traceback.format_exc()[-900:]
            for method, params in (("Profile.stop", {}), ("Profile.watch", {"enabled": False})):
                try:
                    await manager.call(method, **params)
                except Exception:  # noqa: BLE001 - nothing was running
                    pass
    ran = [details[n] for n in cases if n in details]
    return {"pass": bool(ran) and all(c.get("pass") for c in ran), "details": details}


def stamp_out(explicit=None):
    return Path(explicit) if explicit else profiling.default_out("live-" + time.strftime("%Y%m%d-%H%M%S"))


async def run(ctx):
    """Harness entry: the shared session already has the request's save loaded."""
    config = json.loads(Path(ctx.config_path).expanduser().read_text()) if ctx.config_path else {}
    symbols = profiling.GameSymbols(profiling.research_root(None, config))
    cases = os.environ.get("SKYRIM_PROFILER_CASES", "idle,walk,cellload,watch").split(",")
    await wait_playable(ctx.manager)
    return await run_cases(ctx.manager, symbols, cases, float(os.environ.get("SKYRIM_PROFILER_SECONDS", 20)),
                           stamp_out(os.environ.get("SKYRIM_PROFILER_OUT")))


class FakeManager:
    """Just enough of the plugin to run every case without a game: a fixed toy profile, steady frames."""

    def __init__(self):
        frames = [{"m": 0, "rva": 0x1010, "start": 0x1000}, {"m": 1, "rva": 0x10, "sym": "NtWaitForSingleObject"}]
        modules = [{"name": "SkyrimSE.exe", "path": "SkyrimSE.exe", "base": 0, "size": 0x100000, "game": True},
                   {"name": "ntdll.dll", "path": "ntdll.dll", "base": 0, "size": 0x1000, "game": False}]
        hitch = {"frame": 2, "ms": 134.0, "startMs": 16.0, "stacks": [[0], [1, 0]],
                 "samples": [[0.0, 0, 0], [1.0, 1, 1]], "waitChain": None}
        self.profile = {"format": "skyrim-devtools-profile", "version": 1, "hz": 1000.0, "threadsSpec": "all",
                        "durationS": 1.0, "samplerCpuMs": 5.0, "frames": frames, "stacks": [[None, 0], [0, 1]],
                        "modules": modules, "hitchThresholdMs": 100.0, "frameMarks": [[0.0, 16.0, 1]],
                        "threads": [{"tid": t, "name": "", "main": t == 1, "frameThread": t == 1, "samples": 30000,
                                     "walked": 29000, "suspendedMs": 1.0, "runs": [[0.0, 0, 20000, 0], [20.0, 1, 10000, 1]]}
                                    for t in (1, 2, 3)],
                        "hitches": [hitch], "hitchFrames": frames}
        self.hitch = hitch

    async def call(self, method, timeout=None, **params):
        await asyncio.sleep(0)
        if method == "Perf.frameStats":
            return {"frame": 10, "fps": 60.0, "meanMs": 16.7, "p50Ms": 16.7, "p99Ms": 16.8, "maxMs": 17.0, "count": 1200}
        if method == "Events.snapshot":
            return {"playable": True, "menus": []}
        if method == "Profile.stop":
            if params.get("encoding") not in (None, "json"):
                raise RuntimeError("Unexpected parameter 'encoding'")
            text = json.dumps(self.profile)
            return {"bytes": len(text), "chunks": 1, "summary": {}}
        if method == "Profile.read":
            return {"chunk": 0, "data": json.dumps(self.profile), "last": True}
        if method == "Profile.hitches":
            return {"modules": self.profile["modules"], "hitches": [self.hitch], "frames": self.profile["frames"]}
        if method == "Perf.hitches":
            return {"hitches": [{"frame": 2, "ms": 134.0}]}
        return {}

    async def stop(self):
        pass


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SKYRIM_LIVETEST_CONFIG"))
    parser.add_argument("--case", default="idle,walk,cellload,watch")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-<stamp>")
    parser.add_argument("--dry-run", action="store_true", help="run every case against a fake plugin, no game")
    options = parser.parse_args()
    cases = options.case.split(",")
    if options.dry_run:
        global home  # noqa: PLW0603 - the dry run skips the cell changes
        home = no_home
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_cases(FakeManager(), profiling.GameSymbols(), cases, 0.05, Path(tmp))
        print(json.dumps(result))
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
        result = await run_cases(manager, symbols, cases, options.seconds, stamp_out(options.out))
        result["details"]["startS"] = started
    finally:
        await manager.stop()
    # one line, so a harness that reads the last stdout line gets the whole verdict
    print(json.dumps(result))
    return 0 if result["pass"] else 1


async def no_home(manager):
    pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
