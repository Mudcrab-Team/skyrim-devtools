"""Scenario: provoke the alert detectors on the Riverwood fixture through the MCP tool layer.

Each step does something that wastes time on purpose and waits for the matching alert to show up in the
`alerts` field of a tool reply, then undoes it. Thresholds are lowered so the run is short. Prints one
line per detector and the plugin's own per-tick cost.

    python3.13 tools/scenarios/alerts_live.py --config devtools.json [--save Fixture]
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server, load_config  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402

FAST = {"menuOpenS": 6, "combatStalemateS": 8, "swimStallS": 8, "fallTimeS": 1.5, "healCheckS": 2, "targetCheckS": 5,
        "questStalledMin": 0.5, "deathWindowMin": 10}


class Run:
    def __init__(self, server):
        self.server = server
        self.results = {}

    async def tool(self, name, **args):
        out = await self.server.call_tool(name, args)
        out = out[0] if isinstance(out, tuple) else out
        return out[0].text if hasattr(out[0], "text") else str(out[0])

    async def alerts(self):
        return {a["kind"]: a for a in json.loads(await self.tool("agent_alerts"))["active"]}

    async def expect(self, kind, timeout=40):
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            found = (await self.alerts()).get(kind)
            if found:
                # the field must also ride along on an ordinary reply
                reply = await self.tool("snapshot")
                shown = kind in reply and "alerts" in reply
                self.results[kind] = {"ok": True, "afterS": round(time.monotonic() - began, 1), "detail": found["detail"],
                                      "attached": shown}
                print(f"OK   {kind}: {found['detail']} (after {self.results[kind]['afterS']} s, in replies: {shown})", flush=True)
                return True
            await asyncio.sleep(1.0)
        self.results[kind] = {"ok": False}
        print(f"MISS {kind}: not raised within {timeout} s; active {sorted(await self.alerts())}", flush=True)
        return False

    async def gone(self, kind, timeout=30):
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            if kind not in await self.alerts():
                print(f"     {kind} cleared after {round(time.monotonic() - began, 1)} s", flush=True)
                return True
            await asyncio.sleep(1.0)
        print(f"     {kind} still active after {timeout} s", flush=True)
        return False

    async def console(self, command, target="player"):
        return await self.tool("console", command=command, target=target)


async def scenario(config, save):
    manager = GameManager(config)
    recorder = Recorder()
    server = build_server(manager, recorder)
    run = Run(server)
    try:
        started = json.loads(await run.tool("game_start", headless=False, save=save))
        print("started", started.get("phase"), flush=True)
        await run.tool("wait_for", timeout_s=60, playable=True)
        await asyncio.sleep(3)
        await run.tool("agent_alerts", configure={"thresholds": FAST})

        # menuLeftOpen: stand in a menu without sending input
        await run.tool("open_menu", name="Journal Menu")
        await run.expect("menuLeftOpen", 30)
        await run.tool("close_menu", name="Journal Menu")
        await run.gone("menuLeftOpen")

        # actionLoop: the same activate three times with the same outcome
        nearby = json.loads(await run.tool("world_nearby", radius=700, types=["door", "activator"], limit=5))
        refs = [e["refID"] for e in nearby.get("refs", nearby.get("results", [])) if isinstance(e, dict) and e.get("refID")]
        if refs:
            for _ in range(3):
                try:
                    await run.tool("activate", ref_id=refs[0], force=True)
                except Exception as error:  # a refusal repeats identically, which is what the detector measures
                    print("activate:", str(error)[:120], flush=True)
        else:
            for _ in range(3):
                await run.tool("press_control", name="Activate")
        await run.expect("actionLoop", 5)

        # overEncumbered
        await run.console("setav carryweight 1")
        await run.expect("overEncumbered", 15)
        await run.console("setav carryweight 400")
        await run.gone("overEncumbered", 15)

        # lowHealthNoHeal (the fixture has no potions unless the console added some)
        await run.console("damageav health 95")
        await run.expect("lowHealthNoHeal", 20)
        await run.console("restoreav health 9999")

        # crime
        await run.console("modcrimegold 50")
        await run.expect("crime", 15)
        await run.console("paycrimegold")

        # fellOrOutOfBounds: god mode, then far up and let it fall
        await run.console("tgm")
        state = json.loads(await run.tool("player_state"))
        pos = state["pos"]
        await run.console(f"setpos z {pos['z'] + 25000:.0f}")
        await run.expect("fellOrOutOfBounds", 25)
        await asyncio.sleep(4)
        await run.console("tgm")

        # combatStalemate: a hostile with its AI frozen that the player never attacks
        hostile = {"hostileTotal": 0}
        for base in ("EncBandit01Melee", "EncWolf", "EncSabreCat", "EncSkeever"):
            placed = await run.console(f"placeatme {base}")
            print("placeatme", base, placed[:80].replace("\n", " "), flush=True)
            await asyncio.sleep(2)
            hostile = json.loads(await run.tool("combat_state", radius=3000))
            if hostile.get("hostiles"):
                target = hostile["hostiles"][0]["refID"]
                await run.console(f"prid {target}", "none")
                print("tai", await run.console("tai", "selected"), flush=True)
                break
        print("hostiles:", hostile.get("hostileTotal"), flush=True)
        await run.expect("combatStalemate", 30)
        if hostile.get("hostiles"):
            await run.console("kill", "selected")
        await asyncio.sleep(2)

        # deathLoop: die twice
        for _ in range(2):
            await run.console("kill")
            await asyncio.sleep(3)
            await run.tool("reload")
            await run.tool("wait_for", timeout_s=60, playable=True)
            await asyncio.sleep(3)
        await run.expect("deathLoop", 15)

        final = json.loads(await run.tool("agent_alerts", history=True, limit=60))
        print("COST", json.dumps(final.get("cost")), flush=True)
        print("HISTORY", len(final.get("history", [])), "entries", flush=True)
        stats = json.loads(await run.tool("perf_hitches", limit=5))
        print("SLOWCALLS", json.dumps({k: v for k, v in stats.get("slowCalls", {}).items() if "Alerts" in k}), flush=True)
        await run.tool("screenshot", max_width=640)
        print("TIMELINE", recorder.path, flush=True)
    finally:
        print("RESULTS", json.dumps(run.results), flush=True)
        try:
            await run.tool("game_stop")
        except Exception as error:
            print("stop failed:", error, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--save", default="Fixture")
    options = parser.parse_args()
    asyncio.run(scenario(load_config(options.config), options.save))


if __name__ == "__main__":
    main()
