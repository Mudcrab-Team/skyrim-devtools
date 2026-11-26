"""Live check of record/replay and bench_ab: record a 60 s walk in Riverwood, replay it twice, compare the paths,
then A/B benchmark a cheap time-of-day toggle with 3 runs per side.

Module style (`run(ctx)`) on the harness's shared session, started from the Riverwood fixture save. From the command
line `--dry-run` exercises the script against a simulated game, no PC needed:

    python3.13 tests/live/record_replay.py --dry-run
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from skyrim_devtools import bench, recording  # noqa: E402
from skyrim_devtools.use import recording_tools  # noqa: E402

WALK_SECONDS = 60.0
SEGMENT_SECONDS = 5.0
MEDIAN_LIMIT_UNITS = 50.0
NAME = "riverwood-walk"
EVENTS = ("record.started", "record.stopped", "replay.started", "replay.step", "replay.finished")
# A toggle that costs almost nothing and is idempotent: the time of day changes the lighting, not the workload.
CONFIG_A = {"label": "noon", "console": ["set GameHour to 12"]}
CONFIG_B = {"label": "midnight", "console": ["set GameHour to 0"]}


class Rig:
    """What the script needs from the game: native calls, walking, event counts, a clock."""

    def __init__(self, call, walk, turn, events_since, sleep=asyncio.sleep, clock=time.monotonic, has_method=None):
        self.call, self.walk, self.turn, self.events_since = call, walk, turn, events_since
        self.sleep, self.clock, self.has_method = sleep, clock, has_method


async def walk_for(rig, seconds):
    """Walk in a widening polygon, so the path has turns and does not leave the village in one line."""
    end = rig.clock() + seconds
    heading = 0.0
    sent_command = False
    while rig.clock() < end:
        await rig.turn(heading)
        await rig.walk(min(SEGMENT_SECONDS, max(0.5, end - rig.clock())))
        heading = (heading + 50.0) % 360.0
        if not sent_command and rig.clock() > end - seconds + 10:
            # a typed command is part of the recording; this one restates the default time scale
            await rig.call("Console.execute", command="set timescale to 20", target="none")
            sent_command = True


def count_events(rig, cursor):
    items, _ = rig.events_since(cursor)
    counts = {name: 0 for name in EVENTS}
    for event in items:
        if event.get("type") in counts:
            counts[event["type"]] += 1
    return counts


async def check(rig, seconds=WALK_SECONDS, cursor=0):
    details, problems = {}, []
    await rig.call("Recording.configure", loadSettleMs=3000)
    started = await rig.call("Recording.start", name=NAME, intervalMs=100, notes="live check walk")
    details["start"] = {"entry": started.get("entry")}
    await walk_for(rig, seconds)
    stopped = await rig.call("Recording.stop")
    details["recording"] = stopped
    doc = await rig.call("Recording.read", name=NAME)
    recording.validate(doc)
    details["summary"] = recording.summary(doc)
    if not 0.9 * seconds * 1000 <= stopped["durationMs"] <= 1.3 * seconds * 1000:
        problems.append(f"recording length {stopped['durationMs']} ms")
    if details["summary"]["commands"] < 1:
        problems.append("the typed command was not recorded")
    if doc["entry"].get("kind") != "save":
        problems.append(f"entry kind {doc['entry'].get('kind')!r}, wanted save")

    traces = []
    details["replays"] = []
    for index in range(2):
        result = await recording_tools.replay_and_compare(rig.call, NAME, restoreScene=True, settleMs=3000, with_trace=True,
                                                          sleep=rig.sleep, clock=rig.clock)
        traces.append(result.pop("trace", None))
        replay = result["replay"]
        details["replays"].append({"finished": replay.get("finished"), "tier": replay.get("tier"), "error": replay.get("error"),
                                   "durationMs": replay.get("durationMs"), "engineDrift": replay.get("drift"),
                                   "vsRecording": result.get("divergenceVsRecording"), "restore": replay.get("restore"),
                                   "commandsRun": replay.get("commandsRun"), "warnings": replay.get("warnings")})
        if not replay.get("finished"):
            problems.append(f"replay {index + 1} did not finish: {replay.get('error')}")
        else:
            median = result["divergenceVsRecording"]["medianUnits"]
            if median is None or median >= MEDIAN_LIMIT_UNITS:
                problems.append(f"replay {index + 1} median divergence {median} vs recording")
    if all(traces):
        between = recording.divergence(traces[0], traces[1])
        details["replayVsReplay"] = between
        if between["medianUnits"] is None or between["medianUnits"] >= MEDIAN_LIMIT_UNITS:
            problems.append(f"replay-to-replay median divergence {between['medianUnits']}")
    details["events"] = count_events(rig, cursor)
    for name in ("record.started", "record.stopped", "replay.started", "replay.finished"):
        if details["events"][name] < 1:
            problems.append(f"no {name} event")
    if details["events"]["replay.step"] < 10:
        problems.append("fewer than 10 replay.step events")

    report = await bench.bench_ab(rig.call, NAME, CONFIG_A, CONFIG_B, runs=3, has_method=rig.has_method, sleep=rig.sleep,
                                  clock=rig.clock, replay_options={"restoreScene": True, "settleMs": 3000})
    details["bench"] = {"text": bench.render_text(report), "delta": {k: report["delta"][k] for k in report["delta"]},
                        "goodRuns": [report["a"]["goodRuns"], report["b"]["goodRuns"]], "warnings": report["warnings"],
                        "profiler": report["profiler"], "gpuPassDelta": report["gpuPassDelta"]}
    if min(details["bench"]["goodRuns"]) < 3:
        problems.append(f"bench good runs {details['bench']['goodRuns']}")
    for metric in ("p50Ms", "p99Ms", "hitches"):
        if metric not in report["delta"] or report["delta"][metric].get("noiseSd") is None:
            problems.append(f"bench has no noise figure for {metric}")
    return {"pass": not problems, "details": {**details, "problems": problems}}


async def run(ctx):
    client = ctx.client
    cursor = ctx.manager.event_total

    async def walk(seconds):
        await client.walk(direction="forward", duration_ms=int(seconds * 1000))

    async def turn(heading):
        await client.turn_to(heading_deg=heading)

    rig = Rig(ctx.call, walk, turn, ctx.manager.events_since, has_method=ctx.manager.has_method)
    ctx.mark("record walk")
    result = await check(rig, cursor=cursor)
    ctx.mark("done")
    return result


async def dry_run():
    from recording_fake import FakeGame

    game = FakeGame(seed=3, noise=4.0, hour_cost_ms=0.08)
    events = []

    async def call(method, timeout=None, **params):
        out = await game.call(method, **params)
        for name, trigger in (("record.started", "Recording.start"), ("record.stopped", "Recording.stop"),
                              ("replay.started", "Recording.replay"), ("replay.finished", "Recording.replay")):
            if method == trigger:
                events.append({"type": name})
                if name == "replay.started":
                    events.extend({"type": "replay.step"} for _ in range(12))
        return out

    async def instant(_):
        return None

    async def walk(seconds):
        return None

    async def turn(heading):
        return None

    fake_time = [0.0]

    def clock():
        fake_time[0] += 1.0
        return fake_time[0]

    rig = Rig(call, walk, turn, lambda cursor: (events[cursor:], len(events)), sleep=instant, clock=clock, has_method=game.has_method)
    return await check(rig, seconds=60.0)


if __name__ == "__main__":
    if "--dry-run" not in sys.argv:
        sys.exit("run this script through skyrim-devtools-livetest, or pass --dry-run")
    print(json.dumps(asyncio.run(dry_run()), indent=1))
