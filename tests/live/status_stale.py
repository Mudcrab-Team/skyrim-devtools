"""Live check that status and alerts do not outlive what they describe (Riverwood fixture, shared session).

Combat: a hostile fights the player, then the player is moved away; `inCombat` must drop within a few seconds
both through center_on_cell (which clears combat) and through a bare console `coc` (which does not, so the
state has to be derived from live combatants). Stuck: walk into a wall until `moveStuck` is raised, then set a
new navigation goal and load a save; the alert must clear each time.

    python3.13 tests/live/status_stale.py --dry-run
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alerts_live import Run, parse, xyz  # noqa: E402
from skyrim_devtools.use.server import build_server  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402

AWAY = "WhiterunBanneredMare"
BACK = "RiverwoodSleepingGiantInn"
CLEAR_WITHIN_S = 6.0


async def state(run):
    return json.loads(await run.step("player_state", "player_state") or "{}")


async def wait_state(run, want, timeout):
    began = time.monotonic()
    last = {}
    while time.monotonic() - began < timeout:
        last = await state(run)
        if bool(last.get("inCombat")) == want:
            return round(time.monotonic() - began, 1), last
        await asyncio.sleep(0.5 * min(1.0, run.scale))
    return None, last


async def fight(run):
    """A hostile placed next to the player and set on them; returns its ref id when the player is in combat."""
    ref = await run.hostile()
    if not ref:
        return None
    await run.console(f"{ref}.startcombat 00000014", "none")
    after, _ = await wait_state(run, True, 20)
    return ref if after is not None else None


async def combat_case(run, how):
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    await run.console("tgm")
    ref = await fight(run)
    if not ref:
        await run.console("tgm")
        return {"pass": False, "error": "no combat could be started"}
    if how == "center_on_cell":
        moved = parse(await run.step("center_on_cell", "center_on_cell", cell=AWAY) or "{}")
    else:
        await run.console(f"coc {AWAY}", "none")
        moved = {}
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    after, last = await wait_state(run, False, CLEAR_WITHIN_S + 5)
    await run.console("tgm")
    out = {"pass": after is not None and after <= CLEAR_WITHIN_S, "clearedAfterS": after, "combatFlag": last.get("combatFlag"),
           "combatants": last.get("combatants"), "combatCleared": moved.get("combatCleared")}
    await run.step("reload", "reload")
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    return out


async def active_kinds(run):
    return sorted(await run.alerts())


async def gone_within(run, kind, seconds):
    began = time.monotonic()
    while time.monotonic() - began < seconds * max(run.scale, 0.05):
        if kind not in await run.alerts():
            return round(time.monotonic() - began, 1)
        await asyncio.sleep(0.5 * min(1.0, run.scale))
    return None


async def stuck_case(run):
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    raised = False
    for turn in range(6):
        await run.step("walk", "walk", direction="forward", duration_ms=9000, run=True)
        if "moveStuck" in await run.alerts():
            raised = True
            break
        await run.step("turn", "turn_to", heading_deg=(turn * 60) % 360)
    if not raised:
        return {"pass": False, "error": "moveStuck was not raised by walking into walls"}
    pos = xyz((await state(run)).get("pos") or [0, 0, 0])
    nearby = parse(await run.step("nearby", "world_nearby", radius=1500, types=["door", "activator"], limit=5) or "{}")
    refs = [e["refID"] for e in nearby.get("refs", []) if isinstance(e, dict) and e.get("refID")]
    goal = {"refID": refs[0]} if refs else {"x": pos[0] + 300, "y": pos[1], "z": pos[2]}
    await run.step("nav_goto", "nav_goto", goal=goal, timeout_s=40)
    new_goal = await gone_within(run, "moveStuck", 3)
    await run.step("nav_cancel", "nav_cancel")
    # raise it again, then a load must drop it
    await run.step("walk", "walk", direction="forward", duration_ms=9000, run=True)
    again = "moveStuck" in await run.alerts()
    await run.step("reload", "reload")
    await run.step("wait playable", "wait_for", timeout_s=60, playable=True)
    after_load = await gone_within(run, "moveStuck", 6)
    return {"pass": new_goal is not None and (not again or after_load is not None), "clearedByNewGoalS": new_goal,
            "raisedAgain": again, "clearedByLoadS": after_load}


async def run_with(manager, scale=1.0):
    recorder = Recorder()
    server = build_server(manager, recorder)
    run = Run(server, scale)
    details = {}
    try:
        details["combatCenterOnCell"] = await combat_case(run, "center_on_cell")
        details["combatBareCoc"] = await combat_case(run, "coc")
        details["stuck"] = await stuck_case(run)
    finally:
        return {"pass": all(details.get(k, {}).get("pass") for k in ("combatCenterOnCell", "combatBareCoc", "stuck")),
                "details": dict(details, errors=run.errors, timeline=str(recorder.path))}


async def run(ctx):
    return await run_with(ctx.manager)


async def dry():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_use import Harness

    harness = Harness()
    await harness.manager.start(headless=True, save="Fixture")
    return await run_with(harness.manager, scale=0.02)


if __name__ == "__main__":
    if "--dry-run" not in sys.argv:
        raise SystemExit("run this through skyrim-devtools-livetest; --dry-run exercises the script without a game")
    print(json.dumps(asyncio.run(dry())), flush=True)
