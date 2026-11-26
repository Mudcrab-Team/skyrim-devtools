"""Record, replay and A/B benchmark tools (Recording.* in the plugin; see docs/record-replay.md).

These are separate from the `record_*` tools, which write a JSONL trajectory for diagnostics and have no scene.
"""

import asyncio
import json
import time

from .. import bench, recording

POLL_S = 0.5
REPLAY_SLACK_S = 300.0


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def wait_replay(call, run_id, timeout_s, sleep=asyncio.sleep, clock=time.monotonic):
    """Poll Recording.status until replay `run_id` ends; returns its summary (Recording.status lastReplay)."""
    deadline = clock() + timeout_s
    while True:
        status = await call("Recording.status")
        last = status.get("lastReplay")
        if not status.get("replay") and last and last.get("id") == run_id:
            return last
        if clock() > deadline:
            await call("Recording.stopReplay")
            raise TimeoutError(f"replay {run_id} did not finish in {timeout_s:.0f} s")
        await sleep(POLL_S)


async def replay_and_compare(call, path, wait=True, timeout_s=None, sleep=asyncio.sleep, clock=time.monotonic,
                             with_trace=False, **options):
    """Replay a recording and, once done, measure how far the observed poses were from the recorded ones.

    with_trace adds the observed `[tMs, x, y, z, cell]` rows, for comparing two replays with each other."""
    started = await call("Recording.replay", path=path, **{k: v for k, v in options.items() if v is not None})
    if not wait:
        return started
    doc = await call("Recording.read", path=path)
    recording.validate(doc)
    budget = timeout_s or recording.duration_ms(doc) / 1000.0 + REPLAY_SLACK_S
    last = await wait_replay(call, started["id"], budget, sleep=sleep, clock=clock)
    result = {"replay": last}
    if last.get("finished"):
        trace = await call("Recording.replayTrace")
        result["divergenceVsRecording"] = recording.divergence(trace["samples"], doc["samples"])
        if with_trace:
            result["trace"] = trace["samples"]
    return result


def register(mcp, native, manager):
    async def call(method, **params):
        return await native(method, **params)

    @mcp.tool()
    async def recording_start(name: str | None = None, interval_ms: int | None = None, notes: str | None = None,
                              coupling: dict | None = None) -> str:
        """Start capturing a play-through into a portable JSON recording on the test PC: player pose and camera
        point of view every interval_ms (10-1800000, default 50), cell transitions, the console commands typed
        or sent, and the entry point (the save loaded or the coc target, with its age before this call).
        Play by hand or through any other tool, then call recording_stop. `coupling` pins the replay's scene tier,
        for example {"tier": "cell"}; see docs/record-replay.md."""
        return dumps(await call("Recording.start", name=name, intervalMs=interval_ms, notes=notes, coupling=coupling))

    @mcp.tool()
    async def recording_stop() -> str:
        """Stop the recording and write the file; returns its path, sample and event counts and duration."""
        return dumps(await call("Recording.stop"))

    @mcp.tool()
    async def recording_list() -> str:
        """Recordings on the test PC with duration, entry kind and the scene coupling a replay would use."""
        return dumps(await call("Recording.list"))

    @mcp.tool()
    async def replay(path: str | None = None, restore_scene: bool = True, settle_ms: int | None = None,
                     coupling: str | None = None, clean_transition: bool | None = None, force: bool = False,
                     before_run: list[str] | None = None, wait: bool = True, timeout_s: float | None = None) -> str:
        """Replay a recording (a name in the recordings directory, a path, or the newest when omitted). First the
        scene is restored: the entry save is loaded or the coc target entered, a settle delay passes and, for the
        anchored tier, the recorded time of day and weather are set again. Then the player follows the recorded
        poses on the recording's own clock, console commands run at their times and cell changes are reproduced.
        coupling overrides the tier (anchored, cell, worldspace); restore_scene=false only checks the scene;
        force continues after a scene mismatch; before_run are console commands run after the restore, before the
        run (a toggle that survives a load). With wait the reply holds the result and the distance between the
        observed poses and the recorded ones (divergenceVsRecording). Events replay.started/step/finished are
        pushed for aligning a profile or video."""
        options = {"restoreScene": restore_scene, "settleMs": settle_ms, "coupling": coupling,
                   "cleanTransition": clean_transition, "force": force or None, "beforeRun": before_run}
        return dumps(await replay_and_compare(call, path, wait=wait, timeout_s=timeout_s, **options))

    @mcp.tool()
    async def replay_stop() -> str:
        """Stop a running replay."""
        return dumps(await call("Recording.stopReplay"))

    @mcp.tool()
    async def bench_ab(recording: str, a: dict | list[str], b: dict | list[str], runs: int = 3, profile: bool = True,
                       order: str = "interleave", restore_scene: bool = True, settle_ms: int | None = None) -> str:
        """A/B benchmark: replay the same recording `runs` times under configuration A and B and report frame-time
        deltas (p50, p95, p99, hitches, fps and, with the profiler, GPU pass times) with the run-to-run noise.
        A and B are either a list of console commands or {"label", "console": [...], "rpc": [{"method", "params"}],
        "cleanup": {"console": [...], "rpc": [...]}}. Console commands run after the scene restore, right before
        the trajectory; rpc calls (a mod's own tool) are made before the replay. order interleave runs A B A B so
        slow drift hits both. A delta is called real only when it exceeds twice its standard error. Takes
        2 x runs x (recording length + a load), so use a short recording."""
        options = {"restoreScene": restore_scene, "settleMs": settle_ms}
        options = {k: v for k, v in options.items() if v is not None}
        report = await bench.bench_ab(call, recording, a, b, runs=runs, has_method=manager.has_method if manager.methods else None,
                                      profile=profile, order=order, replay_options=options)
        report["text"] = bench.render_text(report)
        return dumps(report)
