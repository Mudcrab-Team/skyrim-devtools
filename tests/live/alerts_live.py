"""Live case: provoke the alert detectors on the Riverwood fixture through the MCP tool layer.

Each step wastes time on purpose and waits for the matching alert to appear in the `alerts` field of a
tool reply, then undoes it. Thresholds are lowered so the run is short. A failing step is recorded and the
run goes on. Runs on the harness's shared session (`run(ctx)`), or on its own game from the command line:

    python3.13 tests/live/alerts_live.py --config devtools.json [--save Fixture]
    python3.13 tests/live/alerts_live.py --dry-run        # no game: exercises the script against a fake
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server, load_config  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402

FAST = {"menuOpenS": 6, "combatStalemateS": 8, "swimStallS": 8, "fallTimeS": 1.5, "healCheckS": 2, "targetCheckS": 5,
        "questStalledMin": 0.5, "deathWindowMin": 10}
AMBIENT = {"HUD Menu", "Cursor Menu", "Fader Menu", "Overlay Interaction Menu", "Overlay Menu", "Mist Menu", "Top Menu",
           "Loading Menu"}


def xyz(pos):
    return pos if isinstance(pos, list) else [pos["x"], pos["y"], pos["z"]]


class Run:
    def __init__(self, server, scale=1.0):
        self.server = server
        self.scale = scale
        self.results = {}
        self.errors = []

    async def tool(self, name, /, **args):
        out = await self.server.call_tool(name, args)
        out = out[0] if isinstance(out, tuple) else out
        return out[0].text if hasattr(out[0], "text") else str(out[0])

    async def step(self, label, name, /, **args):
        """One tool call whose failure is noted, not fatal."""
        try:
            return await self.tool(name, **args)
        except Exception as error:
            self.errors.append(f"{label}: {str(error)[:160]}")
            print("ERR ", label, str(error)[:160], flush=True)
            return ""

    async def alerts(self):
        try:
            return {a["kind"]: a for a in json.loads(await self.tool("agent_alerts"))["active"]}
        except Exception as error:
            self.errors.append(f"agent_alerts: {error}")
            return {}

    async def expect(self, kind, timeout=40):
        timeout *= self.scale
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            found = (await self.alerts()).get(kind)
            if found:
                reply = await self.step("snapshot", "snapshot")
                shown = kind in reply and '"alerts"' in reply
                self.results[kind] = {"ok": True, "afterS": round(time.monotonic() - began, 1), "detail": found["detail"],
                                      "attached": shown}
                print(f"OK   {kind}: {found['detail']} (after {self.results[kind]['afterS']} s, in replies: {shown})", flush=True)
                return True
            await asyncio.sleep(1.0 * min(1.0, self.scale))
        self.results[kind] = {"ok": False, "active": sorted(await self.alerts())}
        print(f"MISS {kind}: not raised within {timeout:g} s; active {self.results[kind]['active']}", flush=True)
        return False

    async def gone(self, kind, timeout=30):
        began = time.monotonic()
        while time.monotonic() - began < timeout * self.scale:
            if kind not in await self.alerts():
                print(f"     {kind} cleared after {round(time.monotonic() - began, 1)} s", flush=True)
                return True
            await asyncio.sleep(1.0 * min(1.0, self.scale))
        print(f"     {kind} still active", flush=True)
        return False

    async def console(self, command, target="player"):
        return await self.step(command, "console", command=command, target=target)

    async def close_menus(self):
        """Leave every non-HUD menu so the game is running again; a menu left open pauses game time."""
        for _ in range(4):
            try:
                open_menus = [m for m in json.loads(await self.tool("list_menus")) if m not in AMBIENT]
            except Exception:
                return
            if not open_menus:
                return
            for menu in open_menus:
                await self.step("close " + menu, "close_menu", name=menu)
            await self.step("cancel", "press_control", name="Cancel")
            await asyncio.sleep(1.0 * min(1.0, self.scale))

    async def hostile(self):
        """Spawn something hostile by form id (the console help lists them) and return its ref id."""
        for term in ("bandit", "wolf", "skeever", "draugr"):
            listing = await self.console(f"help {term} 0", "none")
            ids = [m for line in listing.splitlines() if line.startswith(("NPC_", "ACHR", "LVLN")) for m in re.findall(r"\b([0-9A-Fa-f]{8})\b", line)]
            for form in ids[:4]:
                await self.console(f"placeatme {form}")
                await asyncio.sleep(2)
                state = json.loads(await self.step("combat_state", "combat_state", radius=3000) or "{}")
                if state.get("hostiles"):
                    return state["hostiles"][0]["refID"]
        return None


async def provoke(run):
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    await run.step("thresholds", "agent_alerts", configure={"thresholds": FAST})
    await run.close_menus()

    # menuLeftOpen: stand in a menu without sending input, then really leave it
    await run.step("open journal", "open_menu", name="Journal Menu")
    await run.expect("menuLeftOpen", 30)
    await run.close_menus()
    await run.gone("menuLeftOpen")

    # actionLoop: the same activate three times with the same outcome (a refusal repeated counts too)
    nearby = json.loads(await run.step("nearby", "world_nearby", radius=900, types=["activator"], limit=5) or "{}")
    refs = [e["refID"] for e in nearby.get("refs", []) if isinstance(e, dict) and e.get("refID")]
    for _ in range(3):
        if refs:
            await run.step("activate", "activate", ref_id=refs[0], force=True)
        else:
            await run.step("player_state", "player_state")
    await run.expect("actionLoop", 5)
    await run.close_menus()

    # overEncumbered
    await run.console("setav carryweight 1")
    await run.expect("overEncumbered", 15)
    await run.console("setav carryweight 400")
    await run.gone("overEncumbered", 15)

    # lowHealthNoHeal: drop every potion first so there is nothing to drink
    inventory = json.loads(await run.step("inventory", "player_inventory", filter="potion") or "{}")
    for item in inventory.get("items", []):
        await run.console(f"removeitem {item['formID']} {item.get('count', 1)}")
    await run.console("damageav health 95")
    await run.expect("lowHealthNoHeal", 20)
    await run.console("restoreav health 9999")

    # crime: a bounty by console, else steal something nearby
    await run.console("modcrimegold 50")
    if not await run.expect("crime", 10):
        things = json.loads(await run.step("items", "world_nearby", radius=700, types=["item"], limit=6) or "{}")
        for ref in [e["refID"] for e in things.get("refs", []) if e.get("refID")][:6]:
            await run.step("steal", "activate", ref_id=ref, force=True)
            if await run.expect("crime", 4):
                break
    await run.console("paycrimegold")
    await run.close_menus()

    # fellOrOutOfBounds: god mode, far up, let it fall
    await run.console("tgm")
    state = json.loads(await run.step("player_state", "player_state") or "{}")
    if state.get("pos"):
        x, y, z = xyz(state["pos"])
        await run.console(f"setpos z {z + 25000:.0f}")
    await run.expect("fellOrOutOfBounds", 25)
    await asyncio.sleep(4 * min(1.0, run.scale))
    await run.console("tgm")

    # combatStalemate: a hostile with its AI frozen that the player never attacks
    target = await run.hostile()
    if target:
        await run.console(f"prid {target}", "none")
        await run.console("tai", "selected")
    await run.expect("combatStalemate", 30)
    if target:
        await run.console("kill", "selected")
    await asyncio.sleep(2 * min(1.0, run.scale))

    # deathLoop: die twice
    for _ in range(2):
        await run.console("kill")
        await asyncio.sleep(3 * min(1.0, run.scale))
        await run.step("reload", "reload")
        await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
        await asyncio.sleep(3 * min(1.0, run.scale))
    await run.expect("deathLoop", 15)


async def run_with(manager, scale=1.0):
    recorder = Recorder()
    server = build_server(manager, recorder)
    run = Run(server, scale)
    cost = {}
    try:
        await provoke(run)
        final = json.loads(await run.step("final", "agent_alerts", history=True, limit=60) or "{}")
        cost = final.get("cost") or {}
        print("COST", json.dumps(cost), flush=True)
    finally:
        passed = sum(1 for r in run.results.values() if r.get("ok"))
        ok = passed >= 5 and cost.get("mainMeanUs", 1e9) < 500
        return {"pass": ok, "details": {"detectors": run.results, "passed": passed, "cost": cost,
                                        "errors": run.errors, "timeline": str(recorder.path)}}


async def run(ctx):
    return await run_with(ctx.manager)


async def standalone(config, save):
    manager = GameManager(config)
    try:
        await manager.start(headless=False, save=save)
        await asyncio.sleep(5)
        return await run_with(manager)
    finally:
        try:
            await manager.stop()
        except Exception as error:
            print("stop failed:", error, flush=True)


async def dry():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_use import Harness

    harness = Harness()
    await harness.manager.start(headless=True, save="Fixture")
    return await run_with(harness.manager, scale=0.02)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if options.dry_run:
        result = asyncio.run(dry())
    else:
        config = options.config or __import__("os").environ.get("SKYRIM_LIVETEST_CONFIG")
        result = asyncio.run(standalone(load_config(config), options.save))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
