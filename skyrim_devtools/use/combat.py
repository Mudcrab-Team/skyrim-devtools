"""Combat tools: equip, draw, attack, block, cast, drink, target, and a bounded fight loop.

The primitives wrap the plugin's Combat.* methods. `combat_fight` is a convenience loop over them for
agents that would otherwise spend a model turn per sword swing; every decision it takes is made by
`decide`, a pure function of the Combat.state reply, so the policy is testable without a game.
"""

import asyncio
import json
import time

MELEE_REACH = 170.0
POWER_STAMINA_PCT = 70
CASTER_STYLES = {"mage", "caster"}


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def decide(state, style="melee", drink_below=35, block_incoming=True, have_potion=True):
    """Choose the next action from a Combat.state reply.

    Returns (action, argument): ("done", "clear"|"dead"), ("drink", "health"), ("approach", distance),
    ("block", None), ("cast", None), ("attack", "light"|"power").
    """
    player = state.get("player", {})
    hostiles = state.get("hostiles", [])
    if player.get("dead"):
        return "done", "dead"
    if not hostiles:
        return "done", "clear"
    health = player.get("health", {}).get("pct", 100)
    if have_potion and health < drink_below:
        return "drink", "health"
    # a chosen target that is still alive and in range wins over a nearer bystander
    target = next((h for h in hostiles if h.get("chosen")), hostiles[0])
    reach = MELEE_REACH if style not in CASTER_STYLES else 1400.0
    if style == "ranged":
        reach = 1200.0
    if target["distance"] > reach:
        return "approach", target["distance"]
    incoming = target.get("attack") in ("windup", "swing") and target["distance"] < MELEE_REACH + 90
    if block_incoming and incoming and style == "melee" and player.get("stamina", {}).get("pct", 100) > 15:
        return "block", None
    if style in CASTER_STYLES:
        return "cast", None
    stamina = player.get("stamina", {}).get("pct", 100)
    return "attack", "power" if stamina >= POWER_STAMINA_PCT and target.get("health", {}).get("pct", 100) > 60 else "light"


def register(mcp, native, manager):
    @mcp.tool()
    async def player_equipped() -> str:
        """What the player wears and holds: rightHand, leftHand (weapon, shield or spell), worn armor, voice power."""
        return dumps(await native("Player.equipped"))

    @mcp.tool()
    async def player_spells(kind: str | None = None, limit: int | None = None) -> str:
        """Known spells (kind spell, power, ability, ...) with magicka cost, and shouts."""
        return dumps(await native("Player.spells", kind=kind, limit=limit))

    @mcp.tool()
    async def combat_state(radius: float | None = None, limit: int | None = None) -> str:
        """What a player sees in a fight (Combat.state): own health/magicka/stamina {cur,max,pct}, whether the
        weapon is drawn, blocking, the attack phase, both hands, and the living hostile actors within `radius`
        (default 3000) nearest first: refID, name, distance, bearingDeg relative to where you face (0 =
        straight ahead, positive = to your right), health pct, whether it targets you, its attack phase
        (windup/swing/...) and whether it is blocking."""
        return dumps(await native("Combat.state", radius=radius, limit=limit))

    @mcp.tool()
    async def combat_target(ref_id: str | None = None, radius: float | None = None, face: bool = True,
                            clear: bool = False) -> str:
        """Choose an enemy (the nearest hostile, or ref_id) and turn to face it. The choice is remembered, so
        later combat_attack / combat_cast / combat_block aim at it without a ref_id. clear=true forgets it."""
        return dumps(await native("Combat.target", refID=ref_id, radius=radius, face=face, clear=clear or None))

    @mcp.tool()
    async def combat_face(ref_id: str | None = None) -> str:
        """Turn (yaw and pitch) to look at the chosen target, a given actor, or the nearest hostile."""
        return dumps(await native("Combat.face", refID=ref_id))

    @mcp.tool()
    async def combat_draw(on: bool = True) -> str:
        """Draw (on=true) or sheathe the weapon/hands. Drawing takes about a second; check combat_state."""
        return dumps(await native("Combat.draw", on=on))

    @mcp.tool()
    async def combat_equip(name: str | None = None, form_id: str | None = None, editor_id: str | None = None,
                           hand: str = "right") -> str:
        """Equip a weapon, shield/armor, spell or ammo from the inventory or spell list. `name` is a
        case-insensitive part of its display name (see player_inventory / player_spells). hand: right, left
        or both (two-handers, bows); armor and shields ignore it. Takes effect within a moment."""
        return dumps(await native("Combat.equip", name=name, formID=form_id, editorID=editor_id, hand=hand))

    @mcp.tool()
    async def combat_unequip(hand: str = "right") -> str:
        """Put the weapon or shield in a hand away (fists). Spells cannot be unequipped, only replaced."""
        return dumps(await native("Combat.unequip", hand=hand))

    @mcp.tool()
    async def combat_attack(kind: str = "light", hand: str = "right", face: bool = True, ref_id: str | None = None,
                            hold_ms: int | None = None) -> str:
        """Swing the weapon in `hand` (the attack button, as a keyboard does). kind light taps it; power holds it
        (about 0.9 s) for a power attack that costs more stamina. Faces the chosen target (or the nearest
        hostile) first unless face=false. Returns when the button is released, not when the swing lands;
        poll combat_state for the enemy's health."""
        return dumps(await native("Combat.attack", kind=kind, hand=hand, face=face, refID=ref_id, holdMs=hold_ms))

    @mcp.tool()
    async def combat_block(hold_ms: int = 1500, face: bool = True) -> str:
        """Raise the shield or weapon to block for hold_ms (the left-hand button). With a spell in the left hand
        that button casts instead; equip a shield or unequip the left hand first."""
        return dumps(await native("Combat.block", holdMs=hold_ms, face=face))

    @mcp.tool()
    async def combat_cast(hand: str = "right", hold_ms: int | None = None, face: bool = True,
                          ref_id: str | None = None) -> str:
        """Cast the spell equipped in `hand` at the chosen target. Concentration spells (Flames, Sparks) last
        while the button is held (default 2 s); fire-and-forget spells need a short press (default 0.25 s)."""
        return dumps(await native("Combat.cast", hand=hand, holdMs=hold_ms, face=face, refID=ref_id))

    @mcp.tool()
    async def combat_drink(kind: str | None = None, name: str | None = None, form_id: str | None = None) -> str:
        """Drink a potion. kind health, magicka or stamina picks the weakest potion that covers what is
        missing (or the strongest one); or name / form_id for a specific one. Poisons and food are skipped."""
        return dumps(await native("Combat.drink", kind=kind, name=name, formID=form_id))

    @mcp.tool()
    async def combat_fight(style: str = "melee", rounds: int = 40, drink_below: int = 35, radius: float = 2500,
                           timeout_s: float = 90) -> str:
        """Fight the nearby hostiles with a simple loop until none are left, you die, or rounds/timeout run out:
        face the nearest enemy, walk up to it, block when it winds up an attack, otherwise light/power attack
        (style melee) or cast (style mage), and drink a health potion below drink_below percent health.
        Returns what happened per round summarised, plus your lowest health. Use the primitives instead
        when you want to choose each move yourself."""
        started = time.monotonic()
        log = []
        lowest = 100
        potions = 0
        for number in range(1, rounds + 1):
            if time.monotonic() - started > timeout_s:
                log.append("timeout")
                break
            state = await native("Combat.state", radius=radius, limit=6)
            lowest = min(lowest, state["player"]["health"].get("pct", 100))
            action, arg = decide(state, style=style, drink_below=drink_below)
            if action == "done":
                log.append(arg)
                break
            if action == "drink":
                try:
                    await native("Combat.drink", kind="health")
                    potions += 1
                    log.append("drink")
                except Exception as error:  # no potion left: keep fighting instead of looping on it
                    log.append(f"no potion ({error})")
                    drink_below = -1
                continue
            await native("Combat.target", radius=radius, face=True)
            if action == "approach":
                step = min(max((arg - MELEE_REACH + 60) / 280.0, 0.3), 1.6)
                await manager.call("Move.walk", timeout=20, direction="forward", durationMs=int(step * 1000), run=True)
                log.append("approach")
            elif action == "block":
                await native("Combat.block", holdMs=700)
                log.append("block")
            elif action == "cast":
                await native("Combat.cast", face=True)
                log.append("cast")
            else:
                await native("Combat.attack", kind=arg, face=True)
                log.append(arg)
            await asyncio.sleep(0.35)
        summary = {}
        for item in log:
            summary[item] = summary.get(item, 0) + 1
        final = await native("Combat.state", radius=radius, limit=6)
        return dumps({"outcome": log[-1] if log else "none", "rounds": len(log), "actions": summary,
                      "potionsDrunk": potions, "lowestHealthPct": lowest, "elapsedS": round(time.monotonic() - started, 1),
                      "player": final["player"], "hostilesLeft": final["hostileTotal"], "hostiles": final["hostiles"]})
