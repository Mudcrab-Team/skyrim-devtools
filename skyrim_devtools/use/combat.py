"""Combat tools: equip, draw, attack, block, cast, drink, target, and a bounded fight loop.

The primitives wrap the plugin's Combat.* methods. `combat_fight` is a convenience loop over them for
agents that would otherwise spend a model turn per sword swing; every decision it takes is made by
`decide`, a pure function of the Combat.state reply, so the policy is testable without a game.
"""

import asyncio
import json
import time

from . import ranged

MELEE_REACH = 170.0
POWER_STAMINA_PCT = 70
CASTER_STYLES = {"mage", "caster"}
PASSIVE_MENUS = {"HUD Menu", "Cursor Menu"}


def blocks_play(menu):
    """An open menu blocks play when it pauses the game or takes input; HUD widgets from mods do neither.
    An older plugin reports no flags, so only the known passive menus are skipped then."""
    if not menu.get("open"):
        return False
    if "pausesGame" in menu or "usesMenuContext" in menu:
        return bool(menu.get("pausesGame") or menu.get("usesMenuContext"))
    return menu["name"] not in PASSIVE_MENUS


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def decide(state, style="melee", drink_below=35, block_incoming=True, have_potion=True, have_heal_spell=False,
           retreat_below=0):
    """Choose the next action from a Combat.state reply.

    Returns (action, argument): ("done", "clear"|"dead"), ("drink", "health"), ("heal", "spell"),
    ("retreat", None), ("approach", distance), ("block", None), ("cast", None), ("attack", "light"|"power").
    """
    player = state.get("player", {})
    hostiles = state.get("hostiles", [])
    if player.get("dead"):
        return "done", "dead"
    if not hostiles:
        return "done", "clear"
    health = player.get("health", {}).get("pct", 100)
    if health < drink_below:
        if have_potion:
            return "drink", "health"
        magicka = player.get("magicka", {}).get("pct", 100)
        if have_heal_spell and magicka >= 15:
            return "heal", "spell"
        if health < retreat_below:
            return "retreat", None
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


def assess(state, trades=(), margin=1.6):
    """Judge whether the fight is a bad one: (outmatched, reason).

    Two signals. Level and health: the summed levels of the enemies near us, weighted by their health, against
    our level weighted by ours (margin 1.6 plus a little slack), or one enemy far above our level. Trades: over
    the recorded (our health pct, target health pct) rounds, we lost a lot and the target lost much less.
    """
    player = state.get("player", {})
    level = player.get("level")
    near = [h for h in state.get("hostiles", []) if h.get("distance", 0) < 1500]
    if level and near:
        own = level * max(player.get("health", {}).get("pct", 100), 10) / 100.0
        theirs = sum(h.get("level", 1) * max(h.get("health", {}).get("pct", 100), 10) / 100.0 for h in near)
        if theirs > margin * own + 2:
            return True, f"enemy strength {theirs:.0f} against ours {own:.0f} (levels {[h.get('level') for h in near]} vs {level})"
        top = max(h.get("level", 1) for h in near)
        if top >= level + 10:
            return True, f"an enemy of level {top} against our level {level}"
    if len(trades) >= 5:
        lost = trades[0][0] - trades[-1][0]
        dealt = trades[0][1] - trades[-1][1]
        if lost >= 40 and dealt < lost * 0.5:
            return True, f"losing trades: we lost {lost:.0f}% health, the target {dealt:.0f}%"
    return False, ""


def healing_spells(spells):
    """Self-cast healing spells from a Player.spells reply."""
    found = []
    for spell in spells.get("spells", []):
        name = (spell.get("name") or "").lower()
        if ("healing" in name or "wounds" in name) and not any(word in name for word in ("hands", "other", "aura")):
            found.append(spell["name"])
    return found


def healing_potions(inventory):
    """Count of health-restoring potions in a Player.inventory reply (potion filter), judged by display name."""
    total = 0
    for item in inventory.get("items", []):
        name = (item.get("name") or "").lower()
        if "heal" in name or "health" in name:
            total += item.get("count", 1)
    return total


NO_HEAL_WARNING = ("no healing potion in the inventory and no healing spell: below {drink} percent health there is "
                   "nothing to recover with; add potions (console additem) or avoid this fight")


def register(mcp, native, manager):
    ranged.register(mcp, native, manager)

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
                           timeout_s: float = 90, retreat_below: int = 20, stand_ground: bool = False) -> str:
        """Fight the nearby hostiles with a simple loop until none are left, you die, or rounds/timeout run out:
        face the nearest enemy, walk up to it, block when it winds up an attack, otherwise light/power attack
        (style melee) or cast (style mage). Below drink_below percent health it drinks a health potion, or casts a
        known self-healing spell when it has no potion; with neither it backs away below retreat_below percent and
        returns outcome "retreated" (health low, no way to heal) so you can flee with nav_goto or rest.
        It also judges the fight (enemy levels and health against yours, and trades lost so far) and returns outcome
        "outmatched" with `assessment` before it loses, at the start or mid-fight; stand_ground=true fights on
        regardless. A target it cannot walk to (higher with no navmesh path) is shot with a bow when one is carried
        (see combat_shoot), otherwise it returns outcome "unreachable"; style ranged always shoots. With no healing
        potion and no healing spell it warns at the start (`warnings`, also a `warning` event for events_drain) instead
        of finding out when health runs low. Returns what happened per round summarised, plus your lowest health. Use
        the primitives instead when you want to choose each move yourself."""
        started = time.monotonic()
        log = []
        lowest = 100
        potions = 0
        heals = 0
        retreats = 0
        trades = []
        assessment = None
        healers = []
        try:
            healers = healing_spells(await native("Player.spells", kind="spell"))
        except Exception:  # an older plugin build: potions only
            healers = []
        have_potion = True
        ranged_memory = {}
        unreachable_advice = None
        warnings = []
        potion_count = None
        try:
            potion_count = healing_potions(await native("Player.inventory", filter="potion", limit=200))
        except Exception:  # an older plugin build: found out when the first drink fails
            pass
        if potion_count == 0 and not healers:
            warnings.append(NO_HEAL_WARNING.format(drink=drink_below))
            # buffered as an event right away, so events_drain shows it even if the fight tool is cut short
            manager.note_warning("noHealingPotion", warnings[-1])
        for number in range(1, rounds + 1):
            if time.monotonic() - started > timeout_s:
                log.append("timeout")
                break
            # a tutorial popup ("Health Low") pauses the game, so every swing after it would do nothing
            menus = await manager.call("UI.listMenus")
            blocking = [m["name"] for m in menus if blocks_play(m)]
            if blocking:
                if blocking == ["Tutorial Menu"]:
                    await native("UI.closeMenu", name="Tutorial Menu")
                    log.append("closed tutorial popup")
                    await asyncio.sleep(0.4)
                    continue
                log.append("menu open: " + ", ".join(blocking))
                break
            state = await native("Combat.state", radius=radius, limit=6)
            health = state["player"]["health"].get("pct", 100)
            lowest = min(lowest, health)
            hostiles = state.get("hostiles", [])
            if hostiles:
                target = next((h for h in hostiles if h.get("chosen")), hostiles[0])
                trades.append((health, target.get("health", {}).get("pct", 100)))
                trades = trades[-8:]
            if not stand_ground and hostiles:
                outmatched, reason = assess(state, trades)
                if outmatched:
                    assessment = reason
                    log.append("outmatched")
                    break
            action, arg = decide(state, style=style, drink_below=drink_below, have_potion=have_potion,
                                 have_heal_spell=bool(healers), retreat_below=retreat_below)
            if action == "done":
                log.append(arg)
                break
            if action in ("approach", "attack") and style not in CASTER_STYLES:
                # a target that cannot be walked to (a bridge above, no navmesh path) is shot or reported
                step = await ranged.fight_step(native, manager, state, ranged_memory, number, force=style == "ranged")
                if step:
                    log.append(step["log"])
                    if step.get("stop"):
                        unreachable_advice = step.get("advice")
                        break
                    continue
            if action == "drink":
                try:
                    await native("Combat.drink", kind="health")
                    potions += 1
                    log.append("drink")
                except Exception as error:  # no potion left: heal by spell, back away or keep fighting
                    log.append(f"no potion ({error})")
                    have_potion = False
                continue
            if action == "heal":
                try:
                    await native("Combat.equip", name=healers[0], hand="left")
                    await asyncio.sleep(0.5)
                    await native("Combat.cast", hand="left", holdMs=1800, face=False)
                    heals += 1
                    log.append("heal")
                except Exception as error:
                    log.append(f"heal failed ({error})")
                    healers = []
                await asyncio.sleep(0.4)
                continue
            if action == "retreat":
                retreats += 1
                await native("Combat.target", radius=radius, face=True)
                await manager.call("Move.walk", timeout=20, direction="back", durationMs=1200, run=True)
                log.append("retreat")
                if retreats >= 4:
                    assessment = "health low and nothing to heal with; backing away did not break contact"
                    log.append("retreated")
                    break
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
        result = {"outcome": log[-1] if log else "none", "rounds": len(log), "actions": summary,
                  "potionsDrunk": potions, "healCasts": heals, "lowestHealthPct": lowest,
                  "elapsedS": round(time.monotonic() - started, 1),
                  "player": final["player"], "hostilesLeft": final["hostileTotal"], "hostiles": final["hostiles"]}
        if potion_count is not None:
            result["healingPotions"] = potion_count
        if warnings:
            result["warnings"] = warnings
        if assessment:
            result["assessment"] = assessment
        if ranged_memory.get("shots"):
            result["rangedShots"] = ranged_memory["shots"]
        if unreachable_advice:
            result["advice"] = unreachable_advice
        if result["outcome"] in ("outmatched", "retreated") and not unreachable_advice:
            result["advice"] = ("flee with nav_goto (the enemies keep chasing; a door or a fast travel breaks contact), "
                                "or call combat_fight again with stand_ground=true to fight on")
        return dumps(result)
