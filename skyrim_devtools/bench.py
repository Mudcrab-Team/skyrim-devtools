"""A/B benchmark of a recorded play-through under two configurations (the `bench_ab` flow).

Each run restores the recording's scene, applies one configuration, replays the same trajectory and reads the
frame-time statistics of the trajectory part only; the two configurations alternate (A B A B ...) so slow drift of
the machine (thermals, background work) falls on both. The report gives per-metric means, the run-to-run
standard deviation and whether the difference between A and B stands out of that noise.

A configuration is a dict:
  {"label": "shadows-off",
   "console": ["setini ...", "tgm"],                  # console commands, run after the scene is restored, before the run
   "rpc": [{"method": "Mod.setFeature", "params": {}}],  # plugin or mod tool calls, made before the replay starts
   "cleanup": {"console": [...], "rpc": [...]}}       # undone once, after all runs

Console commands run inside the plugin after any save load, so a toggle held in the save cannot be undone by the
restore; RPC calls are made before the replay and survive only what the restore does not reset.
"""

import asyncio
import math
import time

# (name, higher is better)
METRICS = (("p50Ms", False), ("p95Ms", False), ("p99Ms", False), ("meanMs", False), ("maxMs", False), ("fps", True),
           ("hitches", False), ("gpuMsP50", False))
RESTORE_BUDGET_S = 300.0
POLL_S = 0.25
# A difference counts as real when it exceeds this many standard errors of the run-to-run noise.
SIGMAS = 2.0


class BenchError(RuntimeError):
    pass


def normalize(config, default_label):
    """A configuration dict with every key present; a bare list is taken as console commands."""
    if isinstance(config, (list, tuple)):
        config = {"console": list(config)}
    config = dict(config or {})
    cleanup = dict(config.get("cleanup") or {})
    return {
        "label": str(config.get("label") or default_label),
        "console": [str(c) for c in config.get("console") or []],
        "rpc": [dict(call) for call in config.get("rpc") or []],
        "cleanup": {"console": [str(c) for c in cleanup.get("console") or []],
                    "rpc": [dict(call) for call in cleanup.get("rpc") or []]},
    }


def spread(values):
    """n, mean, sample standard deviation, min, max and the values of a list of numbers."""
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None, "values": []}
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1)) if len(values) > 1 else None
    return {"n": len(values), "mean": round(mean, 4), "sd": None if sd is None else round(sd, 4),
            "min": min(values), "max": max(values), "values": [round(v, 4) for v in values]}


def compare(a, b, higher_is_better=False):
    """Delta of B against A for two `spread` results, with the noise it is judged against."""
    if not a["n"] or not b["n"]:
        return {"delta": None, "verdict": "no data"}
    delta = b["mean"] - a["mean"]
    out = {"a": a["mean"], "b": b["mean"], "delta": round(delta, 4),
           "pct": round(100.0 * delta / a["mean"], 2) if a["mean"] else None}
    if a["n"] < 2 or b["n"] < 2:
        out.update({"noiseSd": None, "significant": None, "verdict": "needs at least 2 runs per side to judge noise"})
        return out
    se = math.sqrt(a["sd"] ** 2 / a["n"] + b["sd"] ** 2 / b["n"])
    out["noiseSd"] = round(math.sqrt((a["sd"] ** 2 + b["sd"] ** 2) / 2.0), 4)
    out["stdError"] = round(se, 4)
    out["significant"] = abs(delta) > SIGMAS * se and delta != 0
    if not out["significant"]:
        out["verdict"] = "within noise"
    else:
        better = (delta > 0) == higher_is_better
        out["verdict"] = "B better" if better else "B worse"
    return out


def frame_metrics(stats, hitches=None, gpu=None):
    """One run's numbers from Perf.frameStats (and optional profiler gpu summary)."""
    out = {"count": stats.get("count"), "meanMs": stats.get("meanMs"), "p50Ms": stats.get("p50Ms"),
           "p95Ms": stats.get("p95Ms"), "p99Ms": stats.get("p99Ms"), "maxMs": stats.get("maxMs"), "fps": stats.get("fps"),
           "hitches": stats.get("hitches")}
    if out["hitches"] is None and hitches is not None:
        out["hitches"] = len(hitches.get("hitches", []))
    if gpu:
        out["gpuMsP50"] = gpu.get("gpuMsP50")
        out["gpuPasses"] = {p["pass"]: p["msPerFrame"] for p in gpu.get("passes", []) if "pass" in p}
    return out


class Profiler:
    """The sampling profiler as a GPU-time source; silently absent when this build or install lacks it."""

    def __init__(self, call, has_method, enabled=True):
        self.call = call
        self.reason = None
        self.active = False
        if not enabled:
            self.reason = "profiler disabled"
            return
        if has_method is not None and not has_method("Profile.start"):
            self.reason = "the plugin has no Profile.start"
            return
        try:
            from . import profiling  # noqa: F401
        except ImportError:
            self.reason = "the profiling module is not installed"
            return
        self.profiling = profiling

    @property
    def available(self):
        return self.reason is None

    async def start(self, seconds):
        if not self.available:
            return
        try:
            # the frame thread at a low rate: GPU pass times need frame boundaries, not stacks
            await self.call("Profile.start", threads="frame", hz=100, durationS=min(600.0, max(10.0, seconds)))
            self.active = True
        except Exception as error:  # noqa: BLE001 - the benchmark goes on without GPU data
            self.reason = f"Profile.start failed: {error}"

    async def stop(self):
        if not self.active:
            return None
        self.active = False
        try:
            raw, _ = await self.profiling.fetch(self.call)
            summary = await asyncio.to_thread(lambda: self.profiling.Profile(raw).summary())
            return summary.get("gpu")
        except Exception as error:  # noqa: BLE001
            self.reason = f"Profile.stop failed: {error}"
            return None


async def apply_calls(call, calls):
    for item in calls:
        await call(item["method"], **(item.get("params") or {}))


async def one_run(call, path, config, profiler, replay_options, duration_s, sleep, clock):
    """Replay once under `config`; returns the run record (never raises for a failed replay)."""
    await apply_calls(call, config["rpc"])
    options = dict(replay_options)
    if config["console"]:
        options["beforeRun"] = config["console"]
    started = await call("Recording.replay", path=path, **options)
    run_id = started["id"]
    run = {"config": config["label"], "replayId": run_id}
    deadline = clock() + duration_s + RESTORE_BUDGET_S
    measuring = False
    while True:
        status = await call("Recording.status")
        active = status.get("replay")
        last = status.get("lastReplay")
        if active and active.get("id") == run_id and active.get("phase") == "run" and not measuring:
            measuring = True
            await call("Perf.reset")
            await profiler.start(duration_s + 30)
        if not active and last and last.get("id") == run_id:
            break
        if clock() > deadline:
            await call("Recording.stopReplay")
            run["error"] = "timeout: the replay did not finish"
            await profiler.stop()
            return run
        await sleep(POLL_S)
    run["replay"] = {k: last.get(k) for k in ("finished", "durationMs", "error", "tier", "cellMismatches", "commandErrors")}
    run["driftMedianUnits"] = (last.get("drift") or {}).get("medianUnits")
    if not last.get("finished"):
        run["error"] = last.get("error") or "the replay was stopped"
        await profiler.stop()
        return run
    if not measuring:
        run["error"] = "the run phase was not observed (replay shorter than the poll interval)"
        await profiler.stop()
        return run
    stats = await call("Perf.frameStats")
    hitches = await call("Perf.hitches", limit=1)
    gpu = await profiler.stop()
    run["metrics"] = frame_metrics(stats, hitches, gpu)
    return run


def summarize_side(runs):
    good = [r for r in runs if "metrics" in r]
    summary = {name: spread([r["metrics"].get(name) for r in good]) for name, _ in METRICS}
    labels = sorted({label for r in good for label in r["metrics"].get("gpuPasses", {})})
    passes = {label: spread([r["metrics"].get("gpuPasses", {}).get(label) for r in good]) for label in labels}
    return {"runs": runs, "goodRuns": len(good), "summary": summary, "gpuPasses": passes}


def build_report(path, a, b, order, runs_a, runs_b, profiler, requested):
    side_a, side_b = summarize_side(runs_a), summarize_side(runs_b)
    delta = {name: compare(side_a["summary"][name], side_b["summary"][name], higher)
             for name, higher in METRICS if side_a["summary"][name]["n"] or side_b["summary"][name]["n"]}
    passes = {label: compare(side_a["gpuPasses"].get(label) or spread([]), side_b["gpuPasses"].get(label) or spread([]))
              for label in sorted(set(side_a["gpuPasses"]) | set(side_b["gpuPasses"]))}
    warnings = []
    for side, label in ((runs_a, a["label"]), (runs_b, b["label"])):
        failed = [r for r in side if "metrics" not in r]
        if failed:
            warnings.append(f"{len(failed)} of {len(side)} runs of {label} failed: {failed[0].get('error')}")
    if not profiler.available:
        warnings.append(f"no GPU pass times: {profiler.reason}")
    p50 = delta.get("p50Ms", {})
    if p50.get("noiseSd") is not None and p50.get("a"):
        noise_pct = 100.0 * p50["noiseSd"] / p50["a"]
        if noise_pct > 5.0:
            warnings.append(f"run-to-run noise is {noise_pct:.1f}% of the p50 frame time: add runs before trusting a small delta")
    return {"recording": path, "order": order, "runsPerSide": requested,
            "a": {"label": a["label"], **side_a}, "b": {"label": b["label"], **side_b},
            "delta": delta, "gpuPassDelta": passes, "profiler": "used" if profiler.available else profiler.reason,
            "warnings": warnings}


def render_text(report):
    """The report as a short table for a terminal or a chat reply."""
    lines = [f"A/B bench of {report['recording']}: {report['a']['label']} (A) vs {report['b']['label']} (B), "
             f"{report['runsPerSide']} runs each, order {report['order']}"]
    lines.append(f"{'metric':10} {'A':>10} {'B':>10} {'delta':>10} {'pct':>8} {'noise sd':>9}  verdict")
    for name, row in report["delta"].items():
        if row.get("delta") is None:
            continue
        pct = "" if row.get("pct") is None else f"{row['pct']:+.1f}%"
        noise = "" if row.get("noiseSd") is None else f"{row['noiseSd']:.3f}"
        lines.append(f"{name:10} {row['a']:>10.3f} {row['b']:>10.3f} {row['delta']:>+10.3f} {pct:>8} {noise:>9}  {row['verdict']}")
    for label, row in report["gpuPassDelta"].items():
        if row.get("delta") is not None:
            lines.append(f"gpu {label[:24]:24} {row['a']:>8.3f} {row['b']:>8.3f} {row['delta']:>+8.3f}  {row['verdict']}")
    lines.extend(f"warning: {w}" for w in report["warnings"])
    return "\n".join(lines)


async def bench_ab(call, recording, a, b, runs=3, has_method=None, profile=True, order="interleave", hitch_ms=50.0,
                   replay_options=None, sleep=asyncio.sleep, clock=time.monotonic, duration_s=None, progress=None):
    """Replay `recording` (a path the plugin can read, or a name in its recordings directory) `runs` times under
    configuration A and `runs` times under B and report frame-time deltas with run-to-run noise.

    `call(method, **params)` is the RPC caller. `order` is `interleave` (A B A B ...) or `blocks` (all A, then all B).
    `duration_s` bounds the wait for each replay; by default it is read from the recording via Recording.list."""
    if runs < 1 or runs > 20:
        raise BenchError("runs is 1 to 20")
    if order not in ("interleave", "blocks"):
        raise BenchError("order is interleave or blocks")
    a, b = normalize(a, "A"), normalize(b, "B")
    if a["label"] == b["label"]:
        b["label"] += "-B"
        a["label"] += "-A"
    if duration_s is None:
        listing = await call("Recording.list")
        duration_s = 0.0
        for row in listing.get("recordings", []):
            if recording in (row.get("name"), row.get("path")) and row.get("durationMs"):
                duration_s = row["durationMs"] / 1000.0
    profiler = Profiler(call, has_method, enabled=profile)
    previous = None
    try:
        await call("Perf.configure", hitchMs=hitch_ms)
    except Exception as error:  # noqa: BLE001 - hitch counts are then absent, frame times still work
        previous = str(error)
    sequence = []
    if order == "interleave":
        for index in range(runs):
            sequence += [("A", a), ("B", b)]
    else:
        sequence = [("A", a)] * runs + [("B", b)] * runs
    results = {"A": [], "B": []}
    try:
        for index, (side, config) in enumerate(sequence):
            run = await one_run(call, recording, config, profiler, replay_options or {}, duration_s or 60.0, sleep, clock)
            run["index"] = index
            results[side].append(run)
            if progress:
                progress(index + 1, len(sequence), side, run)
    finally:
        for config in (a, b):
            try:
                await apply_calls(call, config["cleanup"]["rpc"])
                for command in config["cleanup"]["console"]:
                    await call("Console.execute", command=command, target="none")
            except Exception:  # noqa: BLE001 - a failed cleanup must not hide the measurements
                pass
    report = build_report(recording, a, b, order, results["A"], results["B"], profiler, runs)
    if previous:
        report["warnings"].append(f"Perf.configure failed ({previous}): hitch counts may be missing")
    return report
