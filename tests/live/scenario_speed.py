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


async def main(options):
    manager = GameManager(json.loads(Path(options.config).expanduser().read_text()))
    recorder = Recorder(tempfile.mkdtemp(prefix="scenario-live-"))
    mcp = build_server(manager, recorder)
    details = {}

    async def tool(tool_name, /, **args):
        try:
            result = await mcp.call_tool(tool_name, args)
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}
        content = result[0] if isinstance(result, tuple) else result
        text = content[0].text if content else ""
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def reload():
        await manager.call("Session.reload", timeout=120, wait=True)
        await tool("wait_for", timeout_s=90, playable=True)
        await asyncio.sleep(2)

    if options.dry_run:
        print(json.dumps({"pass": True, "details": {"dry_run": True, "cases": options.cases.split(","),
                                                    "tools": sorted(t.name for t in await mcp.list_tools())}}))
        return
    await manager.start(headless=options.headless, save="Fixture")
    await tool("wait_for", timeout_s=60, playable=True)
    cases = options.cases.split(",")
    try:
        if "speed" in cases:
            walked, mult = {}, {}
            for walk in (100, 200, 300, 400):
                reading = await tool("game_speed", walk=walk)
                mult[walk] = reading.get("speedMult") if isinstance(reading, dict) else reading
                result = await manager.call("Move.walk", timeout=30, direction="forward", durationMs=3000, run=True)
                walked[walk] = result.get("distance") or result
                await asyncio.sleep(0.5)
            reset = await tool("game_speed", reset=True)
            base = walked[100]
            ok = all(isinstance(walked[w], (int, float)) for w in walked) and all(
                walked[w] > base * (w / 100) * 0.7 for w in (200, 300, 400))
            details["speed"] = {"distance3s": walked, "speedMultReadback": mult, "afterReset": reset, "pass": ok,
                                "expect": "distance scales roughly with the percent (within 30%) at 200, 300 and 400"}
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
            chosen = next((q for q in listed if q.get("name") == "The Golden Claw"), (listed or [{}])[0])
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
            runs = {}
            # Whiterun city is its own worldspace; a goal in the player's worldspace outside the gate is what a long walk needs
            home = await tool("nav_places", contains="Riverwood", limit=5)
            home_world = next((p.get("world") for p in home.get("places", []) if p.get("name") == "Riverwood"), None)
            candidates = (await tool("nav_places", contains="Whiterun", limit=40)).get("places", [])
            same = [p for p in candidates if p.get("world") == home_world]
            same.sort(key=lambda p: (0 if "Stable" in p["name"] else 1, p.get("distance", 1e9)))
            goal_place = same[0] if same else None
            details["navGoal"] = goal_place
            if goal_place is None:
                details["nav"] = {"pass": False, "error": "no Whiterun place in the player's worldspace",
                                  "candidates": [(p["name"], p.get("world")) for p in candidates][:10]}
                cases = [c for c in cases if c != "nav"]
        for fast in (None, 200, 300, 400) if "nav" in cases else ():
            await reload()
            args = {"goal": {"place": goal_place["name"]}, "timeout_s": 400}
            if fast:
                args["fast"] = fast
            began = time.time()
            started = await tool("nav_goto", **args)
            samples, last = [], None
            while time.time() - began < 420:
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
        if "nav" in cases:
            normal = runs["100"]
            ok = all(r["outcome"]["state"] == "arrived" for r in runs.values())
            details["nav"] = {"runs": runs, "goal": goal_place["name"],
                              "speedup300": round(normal["seconds"] / max(runs["300"]["seconds"], 0.1), 2),
                              "pass": ok and runs["300"]["seconds"] < normal["seconds"] * 0.6}
        if "travel" in cases:
            await reload()
            before = await manager.call("Game.time")
            markers = await manager.call("Game.mapMarkers", contains="Whiterun", limit=10)
            refused = await tool("fast_travel", to="Whiterun")
            forced = await tool("fast_travel", to="Whiterun", force=True)
            state = await manager.call("Player.state")
            waited = await tool("wait", hours=30)
            await asyncio.sleep(2)
            after = await manager.call("Game.time")
            details["travel"] = {"markers": markers, "unforced": refused, "forced": forced, "cell": state.get("cell"), "waited": waited,
                                 "timeBefore": before, "timeAfter": after,
                                 "pass": isinstance(forced, dict) and "error" not in forced and isinstance(waited, dict)
                                 and after.get("daysPassed", 0) > before.get("daysPassed", 0) + 1}
    finally:
        details["timeline"] = str(recorder.path)
        await manager.stop()
    print(json.dumps({"pass": all(v.get("pass") for k, v in details.items() if isinstance(v, dict) and "pass" in v),
                      "details": details}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--cases", default="speed,lovely,claw,nav,travel")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="build the server and print the plan without a game")
    asyncio.run(main(parser.parse_args()))
