"""Ranged combat: bows, crossbows and projectile spells.

`combat_shoot` equips what it needs, checks the line of sight with an engine ray, aims with ballistic drop and
target lead, holds a full draw, and checks the result by the target's health. Misses correct the next aim from
the arrow's observed path. The maths is in ranged_math (unit tested); the plugin only supplies raw facts
(Ranged.* methods) and presses the same attack button a player does.
"""

import asyncio
import json
import math
import time

from . import ranged_math as rm

SETTLE_S = 0.35
TRACK_MAX_S = 3.0
EQUIP_WAIT_S = 4.0
LOS_SAMPLE_CAP = 48
NAV_WAIT_S = 45.0


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def melee_unreachable(plan, dz):
    """(unreachable, reason) from a Nav.plan reply to the target and its height above the player."""
    if not plan.get("found"):
        return False, ""
    if not plan.get("complete"):
        return True, "no navmesh path to the target" + (f" (it is {dz:.0f} units higher)" if dz > 150 else "")
    return False, ""


class Shooter:
    """One shooting session against one target."""

    def __init__(self, native, manager, target, part="torso", weapon="bow", draw_s=None):
        self.native = native
        self.manager = manager
        self.target = target
        self.part = part
        self.weapon = weapon
        self.draw_s = draw_s
        self.speed = None
        self.gravity = None
        self.speed_known = False
        self.pitch_trim = 0.0
        self.heading_trim = 0.0
        self.launch_bias = None
        self.loadout = None
        self.shots = []
        self.repositions = []

    # -- setup -----------------------------------------------------------------------------------------

    async def prepare(self):
        """Equip and draw. Returns an error dict when the weapon is missing, else None."""
        spell = self.weapon == "spell"
        info = await self.native("Ranged.loadout", spell=True if spell else None, equip=None if spell else True)
        self.loadout = info
        if not info.get("ready"):
            return {"ok": False, "error": "missing", "missing": info.get("missing", []), "loadout": info}
        projectile = (info.get("projectile") if spell else (info.get("ammo") or {}).get("projectile")) or {}
        self.speed = float(projectile.get("speed") or 3000.0)
        self.gravity = float(projectile.get("gravity") or 0.0) * rm.GRAVITY_UNITS
        if not spell:
            deadline = time.monotonic() + EQUIP_WAIT_S
            while info.get("equipQueued") and time.monotonic() < deadline:
                await asyncio.sleep(0.4)
                info = await self.native("Ranged.loadout", equip=None)
                if info.get("bowEquipped") and info.get("ammoEquipped"):
                    break
            else:
                if info.get("equipQueued") and not (info.get("bowEquipped") and info.get("ammoEquipped")):
                    return {"ok": False, "error": "equipFailed", "loadout": info}
            self.loadout = info
            if self.draw_s is None:
                self.draw_s = rm.default_draw_s((info.get("bow") or {}).get("speed"))
        state = await self.native("Ranged.state")
        if not state.get("drawn"):
            await self.native("Combat.draw", on=True)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                if (await self.native("Ranged.state")).get("drawn"):
                    break
        return None

    # -- line of sight ---------------------------------------------------------------------------------

    async def look(self, **extra):
        return await self.native("Ranged.los", refID=self.target, part=self.part, **extra)

    async def suggest(self, los):
        """A firing position within ~600 units that sees the target and is reachable on the navmesh."""
        state = await self.native("Ranged.state")
        here = tuple(state["position"])
        target = tuple(los["target"])
        candidates = rm.sample_positions(here, target)[:LOS_SAMPLE_CAP]
        reply = await self.look(points=[list(c["pos"]) for c in candidates])
        clear = [c for c, r in zip(candidates, reply.get("points", [])) if r.get("clear")]
        for candidate in clear[:8]:
            x, y, z = candidate["pos"]
            plan = await self.native("Nav.plan", x=x, y=y, z=z)
            if plan.get("found") and plan.get("complete") and plan.get("points"):
                end = plan["points"][-1]
                return {"x": round(end[0], 1), "y": round(end[1], 1), "z": round(end[2], 1),
                        "rangeToTarget": round(math.hypot(target[0] - end[0], target[1] - end[1]), 0),
                        "walk": round(candidate["walk"], 0), "candidates": len(candidates), "withLineOfSight": len(clear)}
        return {"candidates": len(candidates), "withLineOfSight": len(clear), "reachable": 0}

    async def go_to(self, spot):
        began = time.monotonic()
        reply = await self.manager.call("Nav.goto", goal={"x": spot["x"], "y": spot["y"], "z": spot["z"]}, mode="input",
                                        timeoutS=NAV_WAIT_S, onCombat="ignore", run=True)
        last = reply
        while time.monotonic() - began < NAV_WAIT_S + 5:
            await asyncio.sleep(0.4)
            status = await self.native("Nav.status")
            if not status.get("active"):
                last = status.get("last") or status
                break
        else:
            await self.native("Nav.cancel")
        return {"state": last.get("state"), "reason": last.get("reason"), "timeS": round(time.monotonic() - began, 1)}

    async def ensure_line(self, reposition, max_repositions):
        """The line of sight reply once it is clear, or an error dict with a suggested position."""
        los = await self.look()
        for attempt in range(max_repositions + 1):
            if los.get("clear"):
                return los, None
            suggestion = await self.suggest(los)
            blocker = {k: los[k] for k in ("hitDistance", "hitLayer", "hitPoint") if k in los}
            if not reposition or attempt >= max_repositions or "x" not in suggestion:
                return los, {"ok": False, "error": "noLineOfSight", "blocker": blocker, "suggestedPosition": suggestion,
                             "repositions": self.repositions}
            moved = await self.go_to(suggestion)
            self.repositions.append({"to": suggestion, **moved})
            los = await self.look()
        return los, {"ok": False, "error": "noLineOfSight", "repositions": self.repositions}

    # -- one shot --------------------------------------------------------------------------------------

    def aim_point(self, los):
        return tuple(los["head"] if self.part == "head" else los["torso"])

    async def velocity_of_target(self, first):
        """Target velocity from two positions 0.12 s apart (aim points are fresh each time anyway)."""
        t0 = time.monotonic()
        await asyncio.sleep(0.12)
        second = await self.look()
        return rm.estimate_velocity([(t0, self.aim_point(first)), (time.monotonic(), self.aim_point(second))]), second

    async def aim(self, los, velocity):
        eye = tuple(los["from"])
        # the shot leaves from the player's current eye; refresh it in case movement changed it
        solution = rm.aim_at(eye, self.aim_point(los), self.speed, self.gravity, velocity)
        heading = solution["heading"] + self.heading_trim
        pitch = solution["pitch"] + self.pitch_trim
        await self.native("Ranged.aim", headingDeg=heading, pitchDeg=pitch)
        return solution, heading, pitch

    async def track(self, known, tof):
        """Samples (t, pos, velocity) per new projectile until it stops or disappears."""
        flights = {}
        began = time.monotonic()
        limit = min(TRACK_MAX_S, tof + 1.2)
        quiet = 0
        while time.monotonic() - began < limit:
            now = time.monotonic() - began
            arrows = (await self.native("Ranged.arrows")).get("arrows", [])
            fresh = [a for a in arrows if a["refID"] not in known]
            for arrow in fresh:
                flights.setdefault(arrow["refID"], []).append((now, tuple(arrow["pos"]), tuple(arrow["velocity"])))
            if flights and all(math.hypot(*s[-1][2]) < 50 for s in flights.values()) and now > 0.3:
                quiet += 1
                if quiet >= 2:
                    break
            elif flights and not fresh:
                break
            await asyncio.sleep(0.04)
        return flights

    async def shoot(self):
        los = await self.look()
        velocity, los = await self.velocity_of_target(los)
        before = los.get("health")
        known = {a["refID"] for a in (await self.native("Ranged.arrows")).get("arrows", [])}
        solution, heading, pitch = await self.aim(los, velocity)
        hold_ms = int((self.draw_s or 0.25) * 1000) if self.weapon != "spell" else 250
        began = time.monotonic()
        if self.weapon == "spell":
            press = asyncio.ensure_future(self.manager.call("Combat.cast", timeout=15, hand="right", holdMs=hold_ms, face=False))
        else:
            press = asyncio.ensure_future(self.manager.call("Combat.attack", timeout=hold_ms / 1000 + 15, kind="light",
                                                           hand="right", face=False, holdMs=hold_ms))
        # keep aiming while the string is held: the target moves and the first aim is a draw old by the release
        while not press.done():
            await asyncio.sleep(0.15)
            if press.done():
                break
            fresh = await self.look()
            solution, heading, pitch = await self.aim(fresh, velocity)
            los = fresh
        reply = await press
        held = time.monotonic() - began
        flights = await self.track(known, solution["time"])
        await asyncio.sleep(SETTLE_S)
        after_los = await self.look()
        after = after_los.get("health")
        dead = bool(after_los.get("dead"))
        damage = max(0.0, (before - after)) if before is not None and after is not None else 0.0
        hit = damage > 0.5 or dead
        shot = {"range": round(solution["range"]), "headingDeg": round(heading, 2), "pitchDeg": round(pitch, 2),
                "predictedFlightS": round(solution["time"], 2), "reachable": solution["reachable"],
                "heldS": round(held, 2), "hit": hit, "damage": round(damage, 1), "arrows": len(flights),
                "targetSpeed": round(math.hypot(*velocity[:2]), 0)}
        if self.weapon != "spell":
            shot["drawMs"] = hold_ms
        self.learn(shot, flights, solution, pitch, hit, tuple(los["from"]))
        self.shots.append(shot)
        return shot, dead

    def learn(self, shot, flights, solution, pitch, hit, origin):
        """Update the model from the arrow's observed path; correct aim after a miss."""
        if not flights:
            return
        samples = max(flights.values(), key=len)
        fit = rm.fit_flight(samples)
        if fit["speed"]:
            shot["observedSpeed"] = round(fit["speed"])
            if not self.speed_known and 200 < fit["speed"] < 20000:
                # the draw, bow and perks all scale the launch speed; the first arrow tells the real value
                self.speed = fit["speed"]
                self.speed_known = True
        if fit["gravity"] and 50 < fit["gravity"] < 3000:
            shot["observedGravity"] = round(fit["gravity"])
            self.gravity = fit["gravity"]
        if fit["elevation"] is not None:
            bias = pitch - fit["elevation"]
            shot["launchBiasDeg"] = round(bias, 2)
            if self.launch_bias is None and abs(bias) < 15:
                self.launch_bias = bias
                self.pitch_trim += bias
        if not hit:
            path = [s[1] for s in samples]
            error = rm.miss_at_range(path, origin, solution["point"])
            if error is not None:
                shot["missVertical"] = round(error[0])
                shot["missLateral"] = round(error[1])
                dp, dh = rm.trim_for_miss(error[0], error[1], solution["range"])
                self.pitch_trim += dp
                self.heading_trim += dh
            else:
                shot["missShort"] = True
                self.pitch_trim += 1.0

    async def run(self, shots, reposition, max_repositions):
        problem = await self.prepare()
        if problem:
            return problem
        los, error = await self.ensure_line(reposition, max_repositions)
        if error:
            return error
        dead = bool(los.get("dead"))
        if dead:
            return {"ok": True, "targetDead": True, "shots": 0, "hits": 0, "note": "target already dead"}
        lost = 0
        for _ in range(shots):
            if dead:
                break
            current = await self.look()
            if not current.get("clear"):
                lost += 1
                los, error = await self.ensure_line(reposition, max_repositions)
                if error:
                    return {**self.summary(dead), "ok": False, "error": error["error"], "suggestedPosition": error.get("suggestedPosition")}
            state = await self.native("Ranged.state")
            if self.weapon != "spell" and state.get("ammoLeft") == 0:
                return {**self.summary(dead), "ok": False, "error": "outOfAmmo"}
            if not state.get("drawn"):
                await self.native("Combat.draw", on=True)
                await asyncio.sleep(1.0)
            _, dead = (await self.shoot())
            await asyncio.sleep(0.2)
        return self.summary(dead)

    def summary(self, dead):
        hits = sum(1 for s in self.shots if s["hit"])
        out = {"ok": True, "weapon": self.weapon, "target": self.target, "shots": len(self.shots), "hits": hits,
               "hitRate": round(hits / len(self.shots), 2) if self.shots else None,
               "damage": round(sum(s["damage"] for s in self.shots), 1), "targetDead": dead,
               "repositions": self.repositions, "model": {"speed": round(self.speed or 0), "gravity": round(self.gravity or 0),
                                                         "pitchTrimDeg": round(self.pitch_trim, 2),
                                                         "headingTrimDeg": round(self.heading_trim, 2)},
               "detail": self.shots}
        if self.draw_s and self.weapon != "spell":
            out["drawS"] = round(self.draw_s, 2)
        return out


async def resolve_target(native, spec, radius=6000):
    """A refID (hex), a part of a hostile's name, or the nearest hostile when spec is empty. (refID, name, state)."""
    state = await native("Combat.state", radius=radius, limit=30)
    hostiles = state.get("hostiles", [])
    text = (str(spec).strip() if spec is not None else "")
    if not text or text.lower() in ("nearest", "chosen"):
        pick = next((h for h in hostiles if h.get("chosen")), hostiles[0] if hostiles else None)
        return (pick["refID"], pick.get("name"), state) if pick else (None, None, state)
    for h in hostiles:
        if h["refID"].lower().lstrip("0x") == text.lower().lstrip("0x") or text.lower() in (h.get("name") or "").lower():
            return h["refID"], h.get("name"), state
    if all(c in "0123456789abcdefABCDEFxX" for c in text):
        return text, None, state
    return None, None, state


async def fight_step(native, manager, state, memory, number, force=False):
    """combat_fight hook for a target out of melee reach. Returns None to carry on with melee, else a dict:
    {"log": text, "stop": bool, "advice": text}. Checks reachability on the navmesh every few rounds and, when the
    target cannot be walked to, shoots if a bow is carried and otherwise reports "unreachable"."""
    hostiles = state.get("hostiles", [])
    if not hostiles:
        return None
    target = next((h for h in hostiles if h.get("chosen")), hostiles[0])
    ref = target["refID"]
    if not force:
        cached = memory.get(ref)
        if cached is None or number - cached["round"] >= 4:
            unreachable, reason = False, ""
            try:
                plan = await native("Nav.plan", refID=ref)
                unreachable, reason = melee_unreachable(plan, target.get("dz", 0))
            except Exception:  # plan unavailable on an older plugin or the cell is loading: keep fighting by melee
                pass
            cached = memory[ref] = {"round": number, "unreachable": unreachable, "reason": reason}
        if not cached["unreachable"]:
            return None
    equipped = (state.get("player", {}).get("rightHand") or {})
    try:
        loadout = await native("Ranged.loadout", equip=None)
    except Exception:
        loadout = {"ready": False}
    if not loadout.get("ready"):
        if force:
            return None
        reason = memory[ref]["reason"] or "target unreachable"
        return {"log": "unreachable", "stop": True,
                "advice": f"{reason}; no bow with arrows to shoot it (missing {loadout.get('missing')}). Get a ranged "
                          "weapon, use a spell, or find another way to it"}
    shooter = Shooter(native, manager, ref)
    result = await shooter.run(shots=2, reposition=True, max_repositions=1)
    memory.setdefault("shots", []).append({k: result.get(k) for k in ("ok", "error", "shots", "hits", "damage", "targetDead")})
    if not result.get("ok") and result.get("error") in ("noLineOfSight", "missing", "equipFailed"):
        return {"log": "unreachable", "stop": True, "advice": f"target unreachable on foot and shooting failed: {result.get('error')}"}
    return {"log": "shoot", "stop": False, "result": result, "equipped": equipped.get("name")}


def register(mcp, native, manager):
    @mcp.tool()
    async def combat_shoot(target: str | None = None, shots: int = 3, draw_s: float | None = None, part: str = "torso",
                           reposition: bool = False, weapon: str = "bow", max_repositions: int = 2) -> str:
        """Shoot a bow (or crossbow) at a target: equips the best bow and arrows from the inventory, or returns
        {"ok":false,"error":"missing","missing":["bow"|"arrows"|"bolts"]}. `target` is a refID, part of a hostile's
        name, or empty for the nearest hostile. Before drawing it casts an engine ray from the eye to the target's
        torso (part "head" for the head): when geometry blocks it (a bridge deck, a wall) it returns error
        "noLineOfSight" with `suggestedPosition` {x,y,z,rangeToTarget}, a spot within ~600 units that sees the
        target and is reachable on the navmesh (range 600-1500 preferred); with reposition=true it walks there
        itself (up to max_repositions) and shoots. Aim includes arrow drop and target movement lead, calibrated
        from the arrows' observed flight; a miss corrects the next shot. Each draw is held draw_s seconds (default
        at least 1.5 s, a full draw: short draws give weak, short arrows). Hits are verified by the target's
        health. Returns shots, hits, hitRate, damage, targetDead, repositions and per-shot detail.
        weapon "spell" shoots the projectile spell equipped in the right hand (a fire bolt; not concentration
        spells) with the same line-of-sight and aim logic."""
        if part not in ("torso", "head"):
            return dumps({"ok": False, "error": "part is torso or head"})
        if weapon not in ("bow", "spell"):
            return dumps({"ok": False, "error": "weapon is bow or spell"})
        shots = min(max(int(shots), 1), 12)
        ref, name, _ = await resolve_target(native, target)
        if not ref:
            return dumps({"ok": False, "error": "noTarget", "target": target})
        shooter = Shooter(native, manager, ref, part=part, weapon=weapon, draw_s=draw_s)
        result = await shooter.run(shots, reposition, min(max(int(max_repositions), 0), 4))
        result["targetName"] = name
        return dumps(result)
