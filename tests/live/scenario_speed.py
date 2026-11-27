#!/usr/bin/env python3.13
"""Live checks for scenario_setup, fast nav, fast_travel and wait (standalone; needs the game host).

  python3.13 tests/live/scenario_speed.py --config mac.json [--cases lovely,claw,nav,travel] [--headless]

Prints one JSON object {"pass": bool, "details": {case: {...}}}; every case lists its measured values.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402


SWEEP = (100, 150, 200, 250, 300, 400)
# keep clear of dens, ruins and forts: the speed comparison needs a walk that animals and bandits do not end
AVOID = ("Barrow", "Cave", "Mine", "Fort", "Ruin", "Shelf", "Tower", "Keep", "Lair", "Camp", "Hold", "Temple", "Nest", "Den",
         "Grove", "Tomb", "Crypt", "Sanctum", "Watchtower", "Pass", "Shrine")
GOAL_MIN, GOAL_MAX, GOAL_TARGET = 4000.0, 20500.0, 8000.0
NORMAL_TIMEOUT_S, FAST_TIMEOUT_S = 240, 150


def make_tool(mcp):
    async def tool(tool_name, /, **args):
        try:
            result = await mcp.call_tool(tool_name, args)
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}
        content = result[0] if isinstance(result, tuple) else result
        # image content (screenshots) has no text; it is not an error
        text = getattr(content[0], "text", "") if content else ""
        try:
            return json.loads(text)
        except ValueError:
            return text

    return tool


def pick_goal(places, home_world):
    """A same-worldspace named place 4000-20500 units away, closest to 8000, without a dangerous-sounding name."""
    ranked = []
    for place in places:
        distance = place.get("distance")
        name = place.get("name") or ""
        if place.get("world") != home_world or not isinstance(distance, (int, float)):
            continue
        if not GOAL_MIN <= distance <= GOAL_MAX or any(word in name for word in AVOID):
            continue
        ranked.append((abs(distance - GOAL_TARGET), place))
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1] if ranked else None


def run_timed_out(run):
    return run["outcome"]["state"] != "arrived" and "imed out" in str(run["outcome"].get("reason"))


def nav_verdict(runs):
    """A timed-out normal run is a lower bound for the normal time, so a 300% arrival under 60% of it still shows
    the speedup; every fast run must arrive."""
    normal, fast = runs["100"], runs["300"]
    arrived = {key: run["outcome"]["state"] == "arrived" for key, run in runs.items()}
    normal_ok = arrived["100"] or run_timed_out(normal)
    speedup = round(normal["seconds"] / max(fast["seconds"], 0.1), 2)
    return {"speedup300": speedup, "normalTimedOut": run_timed_out(normal),
            "pass": bool(normal_ok and all(v for k, v in arrived.items() if k != "100") and fast["seconds"] < normal["seconds"] * 0.6)}


def walked_speed(result):
    """Steady speed (units per second) of a Move.walk reply; the plugin's median after the ramp when present."""
    if not isinstance(result, dict):
        return None
    speed = result.get("speed")
    if isinstance(speed, dict) and speed.get("steadyUnitsPerSecond"):
        return speed["steadyUnitsPerSecond"]
    seconds = result.get("durationMs", 0) / 1000.0
    return result.get("distance", 0) / seconds if seconds > 0 else None


async def case_speed(manager, tool, details):
    """Steady walking speed per requested percent. The distance over a fixed 3 s walk mixes the ramp and any obstacle
    in the way (it clustered at 1400-1600 units for 150-250% where a rock or the slope ended the walk), so the
    median per-poll speed after the ramp is what is compared."""
    steady, distance, mult = {}, {}, {}
    for walk in SWEEP:
        reading = await tool("game_speed", walk=walk)
        mult[walk] = reading.get("speedMult") if isinstance(reading, dict) else reading
        # the movement code picks the new speed up over a moment; measuring straight away reads the old one
        await asyncio.sleep(1.5)
        result = await manager.call("Move.walk", timeout=30, direction="forward", durationMs=4000, run=True, stuck=False)
        steady[walk] = walked_speed(result)
        distance[walk] = result.get("distance") if isinstance(result, dict) else None
        await asyncio.sleep(0.5)
    reset = await tool("game_speed", reset=True)
    base = steady.get(100)
    ratio = {w: round(v / base, 2) for w, v in steady.items() if v and base}
    # the 70% bound applies to every percent; a value outside it is reported with the achieved speed in game_speed `measured`
    ok = bool(base) and all(ratio.get(w, 0) >= (w / 100) * 0.7 for w in (150, 200, 250, 300, 400))
    details["speed"] = {"steadySpeed": steady, "distance4s": distance, "ratioToNormal": ratio, "speedMultReadback": mult,
                        "afterReset": reset, "pass": ok,
                        "expect": "steady speed ratio to the 100% walk is at least 70% of the percent, 150..400"}


async def case_nav(manager, tool, reload, details):
    runs = {}
    home = await tool("nav_places", contains="Riverwood", limit=5)
    home_world = next((p.get("world") for p in home.get("places", []) if p.get("name") == "Riverwood"), None)
    places = (await tool("nav_places", limit=2000)).get("places", [])
    goal_place = pick_goal(places, home_world)
    details["navGoal"] = goal_place
    if goal_place is None:
        details["nav"] = {"pass": False, "error": "no safe named place 4000-20500 units away in the player's worldspace",
                          "count": len(places), "homeWorld": home_world,
                          "candidates": sorted(((p.get("name"), p.get("world"), p.get("distance")) for p in places if p.get("world") == home_world),
                                               key=lambda c: c[2] or 1e12)[:12]}
        return
    for fast in (None, 200, 300, 400):
        await reload()
        args = {"goal": {"place": goal_place["name"]}, "invulnerable": True,
                "timeout_s": NORMAL_TIMEOUT_S if fast is None else FAST_TIMEOUT_S}
        if fast:
            args["fast"] = fast
        began = time.time()
        started = await tool("nav_goto", **args)
        samples, last = [], None
        while time.time() - began < args["timeout_s"] + 20:
            await asyncio.sleep(1.0)
            status = await manager.call("Nav.status")
            pos = (await manager.call("Player.state"))["pos"]
            samples.append(tuple(pos) if isinstance(pos, list) else (pos["x"], pos["y"], pos["z"]))
            if not status["active"]:
                last = status["last"]
                break
        seconds = time.time() - began
        dz = max((abs(b[2] - a[2]) for a, b in zip(samples, samples[1:])), default=0)
        runs[str(fast or 100)] = {"seconds": round(seconds, 1), "maxZStepPerS": round(dz),
                                  "outcome": {k: (last or {}).get(k) for k in ("state", "reason", "stuckCount", "replans", "stalls")},
                                  "speedAfter": await tool("game_speed"), "start": started if isinstance(started, str) else None}
    details["nav"] = {"runs": runs, "goal": goal_place["name"], "goalDistance": goal_place.get("distance"), **nav_verdict(runs)}


async def case_travel(manager, tool, reload, details):
    await reload()
    before = await manager.call("Game.time")
    markers = await manager.call("Game.mapMarkers", contains="Whiterun", limit=10)
    refused = await tool("fast_travel", to="Whiterun")
    forced = await tool("fast_travel", to="Whiterun", force=True)
    state = await manager.call("Player.state")
    mid = await manager.call("Game.time")
    waited = await tool("wait", hours=30)
    # the clock is read again after the engine has had frames to rebuild its calendar from its own counters
    readings = []
    for pause in (0.2, 2.0, 5.0):
        await asyncio.sleep(pause)
        readings.append(await manager.call("Game.time"))
    after = readings[-1]
    gained = after.get("daysPassed", 0) - mid.get("daysPassed", 0)
    stable = all(abs(r.get("daysPassed", 0) - readings[0].get("daysPassed", 0)) < 0.05 for r in readings)
    # the reply is written before the engine rebuilds the global from its counters; they must agree
    consistent = isinstance(waited, dict) and abs(waited.get("daysPassed", 0) - after.get("daysPassed", 0)) < 0.05
    day_moved = (after.get("day"), after.get("month")) != (mid.get("day"), mid.get("month"))
    details["travel"] = {"markers": markers, "unforced": refused, "forced": forced, "cell": state.get("cell"), "waited": waited,
                         "timeBefore": before, "timeAfterTravel": mid, "readings": readings, "daysGained": round(gained, 3), "replyMatchesReading": consistent,
                         "pass": isinstance(forced, dict) and "error" not in forced and isinstance(waited, dict)
                         and 1.1 < gained < 1.4 and stable and consistent and day_moved}


async def run_cases(manager, tool, cases, details):
    async def reload():
        await manager.call("Session.reload", timeout=120, wait=True)
        await tool("wait_for", timeout_s=90, playable=True)
        await asyncio.sleep(2)

    if "speed" in cases:
        await case_speed(manager, tool, details)
    if "lovely" in cases:
        await reload()
        report = await tool("scenario_setup", quest="FreeformRiverwood01", stage=10, position={"ref": "00013491"},
                            kit="light", level=3, time=10, clear_bounty=True)
        state = await manager.call("Player.state")
        quest = await manager.call("Quest.get", editorID="FreeformRiverwood01")
        details["lovely"] = {"report": report, "stage": quest.get("stage"), "level": state.get("level"),
                             "pass": isinstance(report, dict) and quest.get("stage") == 10 and state.get("level") == 3
                             and not report.get("failures")}
    if "claw" in cases:
        await reload()
        found = await tool("quest_find", text="Golden Claw")
        listed = found.get("quests") if isinstance(found, dict) else []
        # MS13FIN is the epilogue quest of the same name and has no stage 10..40
        chosen = next((q for q in listed if q.get("editorID") == "MS13"), (listed or [{}])[0])
        quest_id = chosen.get("editorID")
        details["clawFound"] = listed
        tried = []
        inside = False
        for stage in (10, 20, 30, 40):
            await reload()
            report = await tool("scenario_setup", quest=quest_id, stage=stage, complete_stages_before=True,
                                position={"questTarget": quest_id}, kit="medium", level=10)
            targets = await tool("quest_targets", quest=quest_id)
            text = json.dumps(targets)
            tried.append({"stage": stage, "failures": report.get("failures") if isinstance(report, dict) else report,
                          "warnings": report.get("warnings") if isinstance(report, dict) else None,
                          "mentionsBarrow": "Barrow" in text})
            if "Barrow" in text:
                inside = True
                state = await manager.call("Player.state")
                tried[-1]["player"] = {"cell": state.get("cell"), "pos": state.get("pos"), "level": state.get("level")}
                tried[-1]["quest"] = (await manager.call("Quest.get", editorID=quest_id)).get("stage")
                tried[-1]["targets"] = text[:900]
                await tool("screenshot", max_width=800)
                break
        details["claw"] = {"quest": quest_id, "tried": tried, "pass": inside}
    if "nav" in cases:
        await case_nav(manager, tool, reload, details)
    if "travel" in cases:
        await case_travel(manager, tool, reload, details)


def verdict(details):
    return {"pass": all(v.get("pass") for k, v in details.items() if isinstance(v, dict) and "pass" in v), "details": details}


async def run(ctx):
    """Session style (lovely, claw): the harness already has the game loaded."""
    tool = make_tool(build_server(ctx.manager, Recorder(tempfile.mkdtemp(prefix="scenario-live-"))))
    details = {}
    await run_cases(ctx.manager, tool, ["lovely", "claw"], details)
    return verdict(details)


async def main(options):
    manager = GameManager(json.loads(Path(options.config).expanduser().read_text()))
    recorder = Recorder(tempfile.mkdtemp(prefix="scenario-live-"))
    mcp = build_server(manager, recorder)
    tool = make_tool(mcp)
    details = {}
    if options.dry_run:
        print(json.dumps({"pass": True, "details": {"dry_run": True, "cases": options.cases.split(","),
                                                    "tools": sorted(t.name for t in await mcp.list_tools())}}))
        return
    await manager.start(headless=options.headless, save="Fixture")
    await tool("wait_for", timeout_s=60, playable=True)
    try:
        await run_cases(manager, tool, options.cases.split(","), details)
    finally:
        details["timeline"] = str(recorder.path)
        await manager.stop()
    print(json.dumps(verdict(details), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--cases", default="speed,lovely,claw,nav,travel")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="build the server and print the plan without a game")
    asyncio.run(main(parser.parse_args()))
