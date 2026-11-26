#!/usr/bin/env python3.13
"""Live check of AMD board metrics (ADLX) and GPUPerfAPI hardware counters.

    skyrim-devtools-livetest run REQUEST.yaml          as a harness module: async run(ctx) on the shared session
    python3.13 tests/live/profiler_amd.py --config host.json [--case adlx,gpalist,gpacapture] [--seconds 15]
    python3.13 tests/live/profiler_amd.py --dry-run     every case against a fake plugin, no game

One visible game session in Riverwood. The GPA folder (with GPUPerfAPIDX11-x64.dll) comes from --gpa-dir,
SKYRIM_DEVTOOLS_GPA_DIR or the config key gpaDir. Prints {"pass": bool, "details": {case: {...}}}; each case
lists the checks it failed. Outputs go under --out (default .local/profiles/live-amd-<stamp>).
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiler  # noqa: E402
from profiler import frame_window, home, wait_playable  # noqa: E402
import profiler_gpu  # noqa: E402
from skyrim_devtools import profile_gpa, profile_views, profiling  # noqa: E402
from skyrim_devtools.profile_sqlite import query  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402

# Counters docs/profiling.md lists for RDNA4 D3D11 that the default capture relies on.
DOCUMENTED = ["GPUTime", "GPUBusy", "PSBusy", "VsGsBusy", "CSBusy", "PAStalledOnRasterizerCycles", "L2CacheHit",
              "L0CacheHit", "FetchSize", "WriteSize", "MemUnitBusy", "HiZQuadsCulled"]


async def adlx_case(manager, symbols, seconds, gpa_dir, out):
    await home(manager)
    off = await frame_window(manager, "idle", seconds)
    await manager.call("Profile.start", threads="main", hz=1000, durationS=seconds + 60)
    on = await frame_window(manager, "idle", seconds)
    raw, _ = await profiling.fetch(manager.call)
    raw["context"] = {"phase": "idle", "off": off, "on": on}
    profile = profiling.Profile(raw, symbols)
    paths, summary = profiling.save_outputs(profile, out / "adlx", title="Skyrim SE adlx")
    metrics = raw.get("gpuMetrics") or {}
    board = (summary.get("gpu") or {}).get("board") or {}
    snap = await manager.call("Profile.top", seconds=2)
    top_board = ((snap.get("gpu") or {}).get("board")) or {}
    counters = [c[0] for c in profile_views.timeline_data(profile).get("counters", [])]
    rows = metrics.get("rows") or []
    failed = []
    if metrics.get("source") != "adlx":
        failed.append(f"no ADLX source: {metrics.get('reason')}")
    if len(rows) < seconds * 0.7:
        failed.append(f"{len(rows)} rows in {seconds:g} s, expected about one per second")
    if "gpu_util_pct" not in (metrics.get("supported") or []):
        failed.append("the driver reports no GPU usage")
    if not (board.get("metrics") or {}).get("gpu_clock_mhz", {}).get("max"):
        failed.append("no GPU clock in the report")
    if (metrics.get("sampleUs") or 0) > 50_000:
        failed.append("one ADLX read takes over 50 ms")
    if not top_board.get("metrics"):
        failed.append("Profile.top has no gpu.board")
    if "gpu_clock_mhz" not in counters:
        failed.append("timeline has no board counter rows")
    db = paths.get("sqlite")
    source = dict(query(db, "SELECT key, value FROM Meta WHERE key = 'gpuBoardSource'")["rows"]).get("gpuBoardSource") if db else None
    if source != "adlx":
        failed.append("SQLite Meta lacks gpuBoardSource")
    fps_ratio = round(on["fps"] / off["fps"], 3) if on.get("fps") and off.get("fps") else None
    return {"pass": not failed, "failed": failed, "gpu": metrics.get("gpu"), "supported": metrics.get("supported"),
            "rows": len(rows), "sampleUs": metrics.get("sampleUs"), "board": board.get("metrics"),
            "topBoard": top_board.get("metrics"), "timelineCounters": counters, "fpsRatio": fps_ratio,
            "overhead": summary.get("overhead"), "files": paths}


async def gpalist_case(manager, symbols, seconds, gpa_dir, out):
    if not gpa_dir:
        return {"pass": False, "failed": ["no GPA folder (--gpa-dir, SKYRIM_DEVTOOLS_GPA_DIR or config gpaDir)"]}
    began = time.monotonic()
    result = await profile_gpa.list_counters(manager.call, gpa_dir)
    took = round(time.monotonic() - began, 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gpa-list.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if not result.get("ok"):
        return {"pass": False, "failed": [f"GPA refused: {result.get('error')}"], "seconds": took}
    names = [c.get("name") for c in result.get("counters") or []]
    missing = [n for n in DOCUMENTED if n not in names]
    failed = [f"documented counters missing: {', '.join(missing)}"] if missing else []
    if not result.get("defaultPassCount"):
        failed.append("no pass count for the default set")
    return {"pass": not failed, "failed": failed, "device": result.get("device"), "counters": len(names),
            "names": names, "defaultMissing": result.get("defaultMissing"),
            "defaultPassCount": result.get("defaultPassCount"), "seconds": took}


async def gpacapture_case(manager, symbols, seconds, gpa_dir, out):
    if not gpa_dir:
        return {"pass": False, "failed": ["no GPA folder (--gpa-dir, SKYRIM_DEVTOOLS_GPA_DIR or config gpaDir)"]}
    await home(manager)
    began = time.monotonic()
    result = await profile_gpa.capture(manager.call, gpa_dir)
    took = round(time.monotonic() - began, 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gpu-counters.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    summary = profile_gpa.summarize(result)
    if not summary.get("ok"):
        return {"pass": False, "failed": [f"GPA capture failed: {summary.get('error')}"], "seconds": took,
                "log": summary.get("log")}
    rows = {r["name"]: r for r in summary["counters"]}
    overhead = summary.get("overhead") or {}
    failed = []
    if not (rows.get("GPUTime", {}).get("value") or 0) > 0:
        failed.append("GPUTime is not positive")
    busy = rows.get("GPUBusy", {}).get("value")
    if busy is None or not 0 <= busy <= 100.5:
        failed.append(f"GPUBusy {busy} outside 0-100")
    if not (summary.get("sampleCount") or 0) >= 2:
        failed.append("fewer than 2 samples per frame (clear hooks did not cut passes)")
    if not result.get("passHooks"):
        failed.append("clear hooks were not active during the capture")
    if (summary.get("mismatchedFrames") or 0) > (summary.get("passCount") or 0) // 2:
        failed.append("over half the pass frames had a different clear count")
    if overhead.get("initMs") is None or overhead.get("captureFps") is None:
        failed.append("overhead not reported")
    return {"pass": not failed, "failed": failed, "seconds": took, "report": profile_gpa.render(summary),
            "device": summary.get("device"), "passCount": summary.get("passCount"),
            "framesUsed": summary.get("framesUsed"), "sampleCount": summary.get("sampleCount"),
            "missingCounters": summary.get("missingCounters"), "warnings": summary.get("warnings"),
            "mismatchedFrames": summary.get("mismatchedFrames"),
            "paddedSamples": summary.get("paddedSamples"), "mergedClears": summary.get("mergedClears"),
            "overhead": overhead}


CASES = {"adlx": adlx_case, "gpalist": gpalist_case, "gpacapture": gpacapture_case}


async def run_cases(manager, symbols, names, seconds, gpa_dir, out):
    details = {"symbols": symbols.loaded, "out": str(out), "gpaDir": gpa_dir}
    for name in names:
        try:
            details[name] = await CASES[name](manager, symbols, seconds, gpa_dir, out)
        except Exception as error:  # noqa: BLE001 - one broken case must not hide the others
            details[name] = {"pass": False, "failed": [f"{type(error).__name__}: {error}"[:300]]}
            try:
                await manager.call("Profile.stop")
            except Exception:  # noqa: BLE001 - nothing was running
                pass
    ran = [details[n] for n in names if n in details]
    return {"pass": bool(ran) and all(c.get("pass") for c in ran), "details": details}


def stamp_out(explicit=None):
    return Path(explicit) if explicit else profiling.default_out("live-amd-" + time.strftime("%Y%m%d-%H%M%S"))


async def run(ctx):
    """Harness entry: the shared session already has the request's save loaded."""
    config = json.loads(Path(ctx.config_path).expanduser().read_text()) if ctx.config_path else {}
    symbols = profiling.GameSymbols(profiling.research_root(None, config))
    names = os.environ.get("SKYRIM_PROFILER_CASES", "adlx,gpalist,gpacapture").split(",")
    await wait_playable(ctx.manager)
    return await run_cases(ctx.manager, symbols, names, float(os.environ.get("SKYRIM_PROFILER_SECONDS", 15)),
                           profile_gpa.gpa_dir(None, config), stamp_out(os.environ.get("SKYRIM_PROFILER_OUT")))


class FakeManager(profiler_gpu.FakeManager):
    """The GPU fake plus ADLX board metrics and a GPUPerfAPI device."""

    NAMES = ["gpu_util_pct", "gpu_clock_mhz", "gpu_vram_clock_mhz", "gpu_temp_c", "gpu_hotspot_c", "gpu_power_w",
             "gpu_board_power_w", "gpu_fan_rpm", "gpu_vram_mb", "gpu_voltage_mv"]

    def __init__(self, seconds):
        super().__init__()
        rows = [[i * 1000.0, 95.0, 2900, 1258, 60.0, 75.0, 250.0, 260.0, 1400, 6000, 900]
                for i in range(int(seconds) + 1)]
        self.profile["gpuMetrics"] = {"source": "adlx", "reason": "", "gpu": "Fake Radeon", "names": self.NAMES,
                                      "supported": self.NAMES, "sampleUs": 400.0, "rows": rows}
        self.top["gpu"] = {"utilPct": None, "engines": {}, "vramMb": None,
                           "board": {"source": "adlx", "gpu": "Fake Radeon", "metrics": dict(zip(self.NAMES, rows[0][1:]))}}

    async def call(self, method, timeout=None, **params):
        if method == "Profile.gpaCounters":
            return {"ok": True, "device": "Fake Radeon", "defaultPassCount": 9, "defaultMissing": [],
                    "counters": [{"name": n, "group": "g", "usage": "percentage", "description": ""} for n in DOCUMENTED],
                    "defaultCounters": DOCUMENTED}
        if method == "Profile.gpaCapture":
            return {"ok": True, "device": "Fake Radeon", "passCount": 9, "framesUsed": 69, "sampleCount": 2,
                    "samples": ["frame", "rtv 1920x1080"], "missingCounters": [], "mismatchedFrames": 1,
                    "paddedSamples": 0, "mergedClears": 1, "passHooks": True,
                    "counters": [{"name": "GPUTime", "group": "Timing", "usage": "milliseconds", "values": [2.0, 6.0]},
                                 {"name": "GPUBusy", "group": "Timing", "usage": "percentage", "values": [99.0, 97.0]}],
                    "overhead": {"initMs": 300.0, "cpuUsPerFrame": 150.0, "baselineFrameMsP50": 16.7,
                                 "captureFrameMsP50": 33.0, "baselineFps": 60.0, "captureFps": 30.3}}
        return await super().call(method, timeout=timeout, **params)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SKYRIM_LIVETEST_CONFIG"))
    parser.add_argument("--case", default="adlx,gpalist,gpacapture")
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--gpa-dir", default=None, help="GPA bin folder on the game PC (or env/config)")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-amd-<stamp>")
    parser.add_argument("--dry-run", action="store_true", help="run every case against a fake plugin, no game")
    options = parser.parse_args()
    names = options.case.split(",")
    if options.dry_run:
        global home  # noqa: PLW0603 - the dry run skips the cell changes
        home = profiler.no_home
        import tempfile
        # the ADLX case expects about one board row per second of the window
        seconds = 3.0
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_cases(FakeManager(seconds), profiling.GameSymbols(), names, seconds, "C:\\fake\\gpa", Path(tmp))
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
        result = await run_cases(manager, symbols, names, options.seconds, profile_gpa.gpa_dir(options.gpa_dir, config),
                                 stamp_out(options.out))
        result["details"]["startS"] = started
    finally:
        await manager.stop()
    # one line, so a harness that reads the last stdout line gets the whole verdict
    print(json.dumps(result, default=str))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
