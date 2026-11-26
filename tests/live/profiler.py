#!/usr/bin/env python3.13
"""Live check of the sampling profiler (SPDX-License-Identifier: GPL-3.0-or-later).

    python3.13 tests/live/profiler.py --config host.json [--case idle,walk,cellload,watch] [--seconds 20]

One visible game session in Riverwood. Each profiling case measures frame times for `seconds` without the
profiler, then the same load with it, fetches and symbolises the profile and writes the outputs under --out.
Prints {"pass": bool, "details": {case: {...}}}; each case lists the checks it failed.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools import profiling  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402

INTERIOR = "coc:RiverwoodSleepingGiantInn,Riverwood"
CASES = {
    "idle": {"phase": "idle", "threads": "main"},
    "walk": {"phase": "walk", "threads": "main"},
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
    raw, head = await profiling.fetch(manager.call)
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
    return {
        "pass": not failed, "failed": failed, "off": off, "on": on, "fpsRatio": fps_ratio,
        "overhead": overhead, "coverage": summary["coverage"], "symbolSources": summary["symbolSources"],
        "threads": len(raw.get("threads", [])), "bytes": head.get("bytes"),
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


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--case", default="idle,walk,cellload,watch")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-<stamp>")
    options = parser.parse_args()
    config = json.loads(Path(options.config).expanduser().read_text())
    symbols = profiling.GameSymbols(profiling.research_root(options.symbols, config))
    out = Path(options.out) if options.out else profiling.default_out("live-" + time.strftime("%Y%m%d-%H%M%S"))
    manager = GameManager(config)
    details = {"symbols": symbols.loaded, "out": str(out)}
    try:
        began = time.monotonic()
        await manager.start(headless=False, save=options.save)
        await wait_playable(manager)
        details["startS"] = round(time.monotonic() - began, 1)
        for name in options.case.split(","):
            try:
                if name == "watch":
                    details[name] = await watch_case(manager, symbols, options.seconds, out)
                else:
                    details[name] = await profile_case(manager, symbols, name, CASES[name], options.seconds, out)
            except Exception as error:  # noqa: BLE001 - one broken case must not hide the others
                details[name] = {"pass": False, "failed": [f"{type(error).__name__}: {error}"[:300]]}
                try:
                    await manager.call("Profile.stop")
                    await manager.call("Profile.watch", enabled=False)
                except Exception:  # noqa: BLE001 - nothing was running
                    pass
    finally:
        await manager.stop()
    cases = [details[n] for n in options.case.split(",") if n in details]
    result = {"pass": bool(cases) and all(c.get("pass") for c in cases), "details": details}
    print(json.dumps(result, indent=1))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
