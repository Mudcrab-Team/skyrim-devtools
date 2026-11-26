#!/usr/bin/env python3.13
"""Live check of the profiler's event trace and Perfetto export (SPDX-License-Identifier: GPL-3.0-or-later).

    skyrim-devtools-livetest run REQUEST.yaml          as a harness module: async run(ctx) on the shared session
    python3.13 tests/live/profiler_trace.py --config host.json [--case overhead,capture] [--seconds 30]
    python3.13 tests/live/profiler_trace.py --dry-run   every case against a fake plugin, no game

One visible game session in Riverwood.
  overhead: idle frame rate with the profiler on and trace off, then on, alternating windows; the traced rate
            must stay at or above 95 % of the untraced one (at the game's 60 fps cap).
  capture:  a traced profile of every thread while walking for `seconds`, then a load into an interior and back.
            Checks the trace's threads, engine ranges, D3D11 passes, waits, reads, flows and the Perfetto file.
Prints {"pass": bool, "details": {case: {...}}}; outputs (trace.pftrace included) go under --out, default
.local/profiles/live-trace-<stamp>. They hold game function names: keep them local.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiler  # noqa: E402
from profiler import home, wait_playable  # noqa: E402
import profiler_gpu  # noqa: E402
from skyrim_devtools import profile_trace, profiling  # noqa: E402
from skyrim_devtools.profile_sqlite import query  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402

INTERIOR = "RiverwoodSleepingGiantInn"
MIN_FPS_RATIO = 0.95


def trace_param(config, symbols, api="batched"):
    lists = [p for p in ((config or {}).get("traceHooks"), ROOT / ".local" / "trace-hooks.json", Path.cwd() / ".local" / "trace-hooks.json") if p]
    hooks, unresolved = profile_trace.local_hooks(dict.fromkeys(map(str, lists)), symbols)
    return profile_trace.trace_config(api=api, hooks=hooks), unresolved


async def idle_fps(manager, seconds):
    await manager.call("Perf.reset")
    await asyncio.sleep(seconds)
    stats = await manager.call("Perf.frameStats", window=seconds)
    return {k: stats.get(k) for k in ("fps", "p50Ms", "p99Ms", "count")}


async def overhead_case(manager, symbols, config, seconds, out):
    """Profiler on in both arms so only the trace differs; arms alternate to cancel slow drift."""
    await home(manager)
    trace, _ = trace_param(config, symbols)
    window = max(5.0, seconds / 3)
    arms = {"off": [], "on": []}
    for arm in ("off", "on", "off", "on"):
        params = {"trace": trace} if arm == "on" else {}
        await manager.call("Profile.start", threads="main", hz=1000, durationS=window + 30, **params)
        arms[arm].append(await idle_fps(manager, window))
        await manager.call("Profile.stop")
    fps = {arm: sum(w["fps"] or 0 for w in windows) / len(windows) for arm, windows in arms.items()}
    ratio = round(fps["on"] / fps["off"], 3) if fps["off"] else None
    failed = []
    if ratio is None or ratio < MIN_FPS_RATIO:
        failed.append(f"traced fps {fps['on']:.1f} is {ratio} of untraced {fps['off']:.1f}, under {MIN_FPS_RATIO}")
    return {"pass": not failed, "failed": failed, "fpsRatio": ratio, "fps": {k: round(v, 2) for k, v in fps.items()},
            "windows": arms, "windowS": window}


def perfetto_check(path):
    """Counts from trace_processor when a Python with the `perfetto` package is configured, else None."""
    python = os.environ.get("SKYRIM_DEVTOOLS_PERFETTO_PYTHON")
    if not python or not Path(python).exists():
        return None
    script = (
        "import json, sys\n"
        "from perfetto.trace_processor import TraceProcessor\n"
        "tp = TraceProcessor(trace=sys.argv[1])\n"
        "one = lambda s: [list(r.__dict__.values()) for r in tp.query(s)]\n"
        "print(json.dumps({'slices': one('select count(*) from slice')[0][0],\n"
        " 'flows': one('select count(*) from flow')[0][0],\n"
        " 'samples': one('select count(*) from cpu_profile_stack_sample')[0][0],\n"
        " 'threads': one('select count(distinct utid) from thread_track')[0][0],\n"
        " 'errors': one(\"select name, value from stats where value > 0 and severity = 'error'\")}))\n"
    )
    done = subprocess.run([python, "-c", script, str(path)], capture_output=True, text=True, timeout=600)
    if done.returncode:
        return {"error": done.stderr[-500:]}
    return json.loads(done.stdout.strip().splitlines()[-1])


async def capture_case(manager, symbols, config, seconds, out):
    await home(manager)
    trace, unresolved = trace_param(config, symbols)
    started = await manager.call("Profile.start", threads="all", hz=1000, durationS=seconds + 120, trace=trace)
    walk = asyncio.create_task(profiling.drive(manager, "walk"))
    await asyncio.sleep(seconds)
    walk.cancel()
    load_began = time.monotonic()
    for cell in (INTERIOR, "Riverwood"):
        try:
            await manager.call("Console.execute", command=f"coc {cell}", timeout=60)
        except Exception:  # noqa: BLE001 - the reply can time out while the cell loads
            pass
        await wait_playable(manager)
    load_s = round(time.monotonic() - load_began, 1)
    await asyncio.sleep(2)
    raw, transfer = await profiling.fetch(manager.call)
    raw["context"] = {"phase": f"walk {seconds:g} s, then coc {INTERIOR} and back"}
    profile = profiling.Profile(raw, symbols)
    paths, _ = profiling.save_outputs(profile, out / "capture", title="Skyrim SE trace")
    summary = profile_trace.summary(raw)
    _, stats = profile_trace.build_perfetto(profile)
    decoded = profile_trace.decode(raw) or {"threads": [], "names": []}
    categories = {}
    named_threads = 0
    for thread in decoded["threads"]:
        named_threads += bool(thread["name"])
        for _, _, name_id, _, _, _ in thread["events"]:
            category = decoded["names"][name_id][1]
            categories[category] = categories.get(category, 0) + 1
    hooks = (started.get("trace") or {}).get("hooks", [])
    installed = [h["label"] for h in hooks if not h.get("error")]
    builtin = {h["label"] for h in profile_trace.DEFAULT_HOOKS}
    db = paths["sqlite"]
    recipes = {}
    doc = (ROOT / "docs" / "profiling.md").read_text(encoding="utf-8")
    section = doc[doc.index("## Perfetto trace"):doc.index("## SQLite export")]
    for index, sql in enumerate(section.split("```sql\n")[1:]):
        result = query(db, sql.split("```")[0], limit=5)
        recipes[f"recipe{index + 1}"] = {"columns": result["columns"], "rows": result["rows"]}
    tp = perfetto_check(paths["perfetto"])
    failed = []
    if not summary.get("traced"):
        failed.append("the profile has no trace")
    dropped = summary.get("droppedEvents") or 0
    if dropped > 0.01 * max(1, (summary.get("events") or 0) + dropped):
        failed.append(f"{dropped} events dropped, over 1 %")
    if len(builtin & set(installed)) < 6:
        failed.append(f"only {len(builtin & set(installed))} of {len(builtin)} built-in hooks installed")
    if not (summary.get("api") or {}).get("installed"):
        failed.append(f"D3D11 hooks not installed: {(summary.get('api') or {}).get('reason')}")
    if not ((summary.get("drawsPerFrame") or {}).get("p50") or 0) > 0:
        failed.append("no draw calls per frame")
    if len(decoded["threads"]) < 4 or named_threads < 2:
        failed.append(f"{len(decoded['threads'])} traced threads, {named_threads} named")
    for category, label in (("render", "engine render ranges"), ("pass", "D3D11 passes"), ("wait", "OS waits"),
                            ("io", "file reads"), ("cell", "cell attach/detach ranges")):
        if not categories.get(category):
            failed.append(f"no {label}")
    if not stats.get("flows"):
        failed.append("no submit-to-GPU flows")
    if not stats.get("samples"):
        failed.append("no call-stack samples in the Perfetto trace")
    if not any(r["rows"] for r in recipes.values()):
        failed.append("the documented SQL recipes return nothing")
    if tp is not None and (tp.get("error") or tp.get("errors") or not tp.get("flows") or not tp.get("slices")):
        failed.append(f"trace_processor: {tp}")
    return {"pass": not failed, "failed": failed, "pftrace": paths["perfetto"], "files": paths,
            "bytes": Path(paths["perfetto"]).stat().st_size, "perfetto": stats, "traceProcessor": tp,
            "categories": categories, "threads": len(decoded["threads"]), "namedThreads": named_threads,
            "hooksInstalled": installed, "hookErrors": [h for h in hooks if h.get("error")], "unresolvedLocal": unresolved,
            "summary": {k: summary.get(k) for k in ("events", "droppedEvents", "frames", "phases", "drawsPerFrame",
                                                    "apiPasses", "waits", "reads", "api", "settings")},
            "recipes": recipes, "loadS": load_s, "transfer": transfer.get("transfer")}


CASES = {"overhead": overhead_case, "capture": capture_case}


async def run_cases(manager, symbols, config, names, seconds, out):
    details = {"symbols": symbols.loaded, "out": str(out)}
    for name in names:
        try:
            details[name] = await CASES[name](manager, symbols, config, seconds, out)
        except Exception as error:  # noqa: BLE001 - one broken case must not hide the others
            details[name] = {"pass": False, "failed": [f"{type(error).__name__}: {error}"[:300]]}
            try:
                await manager.call("Profile.stop")
            except Exception:  # noqa: BLE001 - nothing was running
                pass
    ran = [details[n] for n in names if n in details]
    return {"pass": bool(ran) and all(c.get("pass") for c in ran), "details": details}


def stamp_out(explicit=None):
    return Path(explicit) if explicit else profiling.default_out("live-trace-" + time.strftime("%Y%m%d-%H%M%S"))


async def run(ctx):
    """Harness entry: the shared session already has the request's save loaded."""
    config = json.loads(Path(ctx.config_path).expanduser().read_text()) if ctx.config_path else {}
    symbols = profiling.GameSymbols(profiling.research_root(None, config))
    names = os.environ.get("SKYRIM_PROFILER_CASES", "overhead,capture").split(",")
    await wait_playable(ctx.manager)
    return await run_cases(ctx.manager, symbols, config, names, float(os.environ.get("SKYRIM_PROFILER_SECONDS", 30)),
                           stamp_out(os.environ.get("SKYRIM_PROFILER_OUT")))


class FakeManager(profiler_gpu.FakeManager):
    """The GPU fake plus a trace in the profile when Profile.start asked for one."""

    def __init__(self):
        super().__init__()
        sys.path.insert(0, str(ROOT / "tests"))
        from test_profile_trace import traced_raw

        self.traced = traced_raw()
        self.with_trace = False

    async def call(self, method, timeout=None, **params):
        if method == "Profile.start":
            self.with_trace = "trace" in params
            hooks = [{"label": h["label"], "rva": 4096 + i} for i, h in enumerate(params.get("trace", {}).get("hooks", []))]
            return {"started": True, "trace": {"hooks": hooks}} if self.with_trace else {"started": True}
        if method in ("Profile.stop", "Profile.read"):
            self.profile.update({k: self.traced[k] for k in ("gpu", "frameMarks", "marks", "counters", "pid")})
            if self.with_trace:
                self.profile["trace"] = dict(self.traced["trace"], threads=[
                    dict(t, name=t["name"] or f"thread {t['tid']}", dropped=0) for t in self.traced["trace"]["threads"]] + [
                    {"tid": t, "name": f"worker {t}", "count": 0, "dropped": 0, "shortWaits": 0, "events": ""} for t in (2, 3)])
            else:
                self.profile.pop("trace", None)
        return await super().call(method, timeout=timeout, **params)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SKYRIM_LIVETEST_CONFIG"))
    parser.add_argument("--case", default="overhead,capture")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout (default: config researchCheckout)")
    parser.add_argument("--out", default=None, help="default .local/profiles/live-trace-<stamp>")
    parser.add_argument("--dry-run", action="store_true", help="run every case against a fake plugin, no game")
    options = parser.parse_args()
    names = options.case.split(",")
    if options.dry_run:
        global home, wait_playable  # noqa: PLW0603 - the dry run skips the cell changes
        home = profiler.no_home
        import tempfile

        async def playable(manager, timeout=0):
            return True

        wait_playable = playable
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_cases(FakeManager(), profiling.GameSymbols(), {}, names, 0.01, Path(tmp))
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
        result = await run_cases(manager, symbols, config, names, options.seconds, stamp_out(options.out))
        result["details"]["startS"] = started
    finally:
        await manager.stop()
    # one line, so a harness that reads the last stdout line gets the whole verdict
    print(json.dumps(result, default=str))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
