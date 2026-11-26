"""Scenario: A Lovely Letter, Sven's side (lie to Camilla with Sven's forged letter).

Replays the playthrough with the same RPC calls the MCP tools make and asserts the quest's final stage.
Needs a save that starts in Riverwood with the quest unstarted and an unmodified Faendal, Sven and Camilla.

    python3.13 tools/scenarios/a_lovely_letter_sven.py --config devtools.json [--save Fixture]
"""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402

QUEST = "FreeformRiverwood01"
FAENDAL, SVEN, CAMILLA = "0001348C", "00013491", "00013488"
TRADER_DOOR_OUTSIDE = "0001341F"
RUN_SPEED = 280.0  # units per second at a run; walks are re-aimed every leg so drift stays small


class Scenario:
    def __init__(self, manager):
        self.m = manager

    async def call(self, method, **params):
        return await self.m.call(method, **params)

    async def pos(self):
        state = await self.call("Player.state")
        return state["pos"], state["rotDeg"]["yaw"], state["cell"]

    async def ref_pos(self, ref):
        return (await self.call("Ref.get", refID=ref))["pos"]

    async def walk(self, target, stop=100.0, legs=16):
        for _ in range(legs):
            (x, y, _), yaw, _cell = await self.pos()
            dx, dy = target[0] - x, target[1] - y
            dist = math.hypot(dx, dy)
            if dist < stop:
                return dist
            turn = (math.degrees(math.atan2(dx, dy)) - yaw + 540) % 360 - 180
            if abs(turn) > 4:
                await self.call("Player.look", yawDeg=turn)
            hold = max(int(min(dist - stop, 600) / RUN_SPEED * 1000), 200)
            await self.call("Input.key", key="W", holdMs=hold)
            await asyncio.sleep(hold / 1000 + 0.3)
        raise AssertionError(f"could not reach {target}")

    async def talk(self, ref, *choices):
        """Start a conversation and pick one topic per entry (substring match); returns the lines heard."""
        await self.call("Dialogue.start", refID=ref, wait=True)
        heard = []
        state = await self.call("Dialogue.waitIdle", skip=True)
        for choice in choices:
            topics = [t["text"] for t in state["topics"]]
            match = [t for t in state["topics"] if choice.lower() in t["text"].lower()]
            assert match, f"topic {choice!r} not offered, have {topics}"
            await self.call("Dialogue.select", index=match[0]["index"])
            state = await self.call("Dialogue.waitIdle", skip=True)
            heard += [line["text"] for line in state.get("lines", [])]
        await self.call("Dialogue.close")
        return heard

    async def stage(self):
        return (await self.call("Quest.get", editorID=QUEST))["stage"]

    async def use_door(self, ref, stop=60.0):
        await self.walk((await self.ref_pos(ref))[:2], stop=stop)
        for _ in range(8):
            seen = (await self.call("World.crosshair")).get("ref")
            if seen and seen["refID"].upper() == ref:
                break
            await self.call("Input.key", key="W", holdMs=120)
            await asyncio.sleep(0.4)
        else:
            raise AssertionError("door never under the crosshair")
        await self.call("Input.control", name="Activate")
        await asyncio.sleep(3)

    async def run(self):
        assert await self.stage() == 0, "quest must be unstarted"
        await self.walk((await self.ref_pos(FAENDAL))[:2], stop=130)
        await self.talk(FAENDAL, "problem with Sven", "hope")
        assert await self.stage() == 10, "Faendal's letter not accepted"
        await self.walk((await self.ref_pos(SVEN))[:2], stop=130)
        await self.talk(SVEN, "Faendal wanted me to deliver")
        assert await self.stage() == 105, "Sven's counter-letter not received"
        await self.use_door(TRADER_DOOR_OUTSIDE)
        _pos, _yaw, cell = await self.pos()
        assert cell["interior"], "did not enter the shop"
        await self.walk((await self.ref_pos(CAMILLA))[:2], stop=110)
        await self.talk(CAMILLA, "(Lie)")
        stage = await self.stage()
        assert stage == 230, f"lie was not accepted, stage {stage}"
        return stage


async def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--save", default="Fixture")
    options = parser.parse_args(argv)
    manager = GameManager(json.loads(Path(options.config).expanduser().read_text()))
    await manager.start(headless=False, save=options.save)
    try:
        stage = await Scenario(manager).run()
        print(json.dumps({"scenario": "a_lovely_letter_sven", "passed": True, "stage": stage}))
        return 0
    except AssertionError as error:
        print(json.dumps({"scenario": "a_lovely_letter_sven", "passed": False, "error": str(error)}))
        return 1
    finally:
        await manager.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
