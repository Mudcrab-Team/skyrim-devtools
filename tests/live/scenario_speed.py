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

    async def tool(name, **args):
        try:
            result = await mcp.call_tool(name, args)
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

    await manager.start(headless=options.headless, save="Fixture")
    await tool("wait_for", timeout_s=60, playable=True)
    cases = options.cases.split(",")
    try:
        if "speed" in cases:
            walked = {}
            for walk in (100, 200, 300, 400):
                await tool("game_speed", walk=walk)
                result = await manager.call("Move.walk", timeout=30, direction="forward", durationMs=3000, run=True)
                walked[walk] = result.get("distance") or result
                await asyncio.sleep(0.5)
            reset = await tool("game_speed", reset=True)
            details["speed"] = {"distance3s": walked, "afterReset": reset,
                                "pass": isinstance(walked[300], (int, float)) and walked[300] > 1.5 * walked[100]}
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
            quest_id = ((found.get("quests") or [{}])[0].get("editorID")) if isinstance(found, dict) else None
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
                    tried[-1]["targets"] = text[:900]
                    await tool("screenshot", max_width=800)
                    break
            details["claw"] = {"quest": quest_id, "tried": tried, "pass": inside}
        if "nav" in cases:
            runs = {}
            for fast in (None, 200, 300, 400):
                await reload()
                args = {"goal": {"place": "Whiterun"}, "timeout_s": 400}
                if fast:
                    args["fast"] = fast
                began = time.time()
                started = await tool("nav_goto", **args)
                samples, last = [], None
                while time.time() - began < 420:
                    await asyncio.sleep(1.0)
                    status = await manager.call("Nav.status")
                    pos = (await manager.call("Player.state"))["pos"]
                    samples.append((pos["x"], pos["y"], pos["z"]))
                    if not status["active"]:
                        last = status["last"]
                        break
                seconds = time.time() - began
                dz = max((abs(b[2] - a[2]) for a, b in zip(samples, samples[1:])), default=0)
                runs[str(fast or 100)] = {"seconds": round(seconds, 1), "maxZStepPerS": round(dz),
                                          "outcome": {k: (last or {}).get(k) for k in ("state", "reason", "stuckCount", "replans", "stalls")},
                                          "speedAfter": await tool("game_speed"), "start": started if isinstance(started, str) else None}
            normal = runs["100"]
            ok = all(r["outcome"]["state"] == "arrived" for r in runs.values())
            details["nav"] = {"runs": runs, "speedup300": round(normal["seconds"] / max(runs["300"]["seconds"], 0.1), 2),
                              "pass": ok and runs["300"]["seconds"] < normal["seconds"] * 0.6}
        if "travel" in cases:
            await reload()
            before = await manager.call("Game.time")
            refused = await tool("fast_travel", to="Whiterun")
            forced = await tool("fast_travel", to="Whiterun", force=True)
            state = await manager.call("Player.state")
            waited = await tool("wait", hours=30)
            await asyncio.sleep(2)
            after = await manager.call("Game.time")
            details["travel"] = {"unforced": refused, "forced": forced, "cell": state.get("cell"), "waited": waited,
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
    asyncio.run(main(parser.parse_args()))
