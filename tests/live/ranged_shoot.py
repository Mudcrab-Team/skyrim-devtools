"""Live check of combat_shoot: flat ground at ~1000 units and an archer standing on the Riverwood bridge deck.

Runs against the shared session (module style, `run(ctx)`), starting from the Riverwood fixture. A frozen bandit
stands in for the archer so nothing shoots back. Returns {"pass", "details"} with measured numbers.
"""
import asyncio
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use import ranged

BOWS = ("HuntingBow", "LongBow")
ARROWS = "IronArrow"
CANDIDATES = ("EncBandit01Missile", "EncBandit02Missile", "EncBandit01Melee", "EncBandit03Melee")
MAX_SHOTS = 8
MIN_HIT_RATE = 0.5
MIN_DRAW_S = 1.5


def xyz(pos):
    """Player.state and World.models give positions as [x, y, z]; accept a dict too."""
    return tuple(pos) if isinstance(pos, (list, tuple)) else (pos["x"], pos["y"], pos["z"])


def clean(params):
    return {k: v for k, v in params.items() if v is not None}


async def spawn(ctx, native, where):
    """Place a bandit at `where` (x, y, z), freeze its AI, return its refID."""
    before = {r["refID"] for r in (await native("World.nearby", radius=3000, types=["actor"], limit=60))["refs"]}
    for base in CANDIDATES:
        await native("Console.execute", command=f"placeatme {base}", target="player")
        await asyncio.sleep(1.5)
        refs = (await native("World.nearby", radius=3000, types=["actor"], limit=60))["refs"]
        fresh = [r for r in refs if r["refID"] not in before and not r.get("dead")]
        if fresh:
            ref = fresh[0]["refID"]
            await native("Console.execute", command=f"prid {ref}", target="none")
            for axis, value in zip("xyz", where):
                await native("Console.execute", command=f"setpos {axis} {value:.0f}", target="selected")
            await native("Console.execute", command="tai", target="selected")
            await asyncio.sleep(2.0)
            return ref, base
    return None, None


async def kill(native, ref):
    if ref:
        await native("Console.execute", command=f"prid {ref}", target="none")
        await native("Console.execute", command="kill", target="selected")


async def case(ctx, native, label, where, expect_reposition):
    ref, base = await spawn(ctx, native, where)
    if not ref:
        return {"label": label, "pass": False, "error": "could not place a target"}
    ctx.mark(f"{label}: target {ref} ({base})")
    first = await native("Ranged.los", refID=ref)
    shooter = ranged.Shooter(native, ctx.manager, ref)
    result = await shooter.run(MAX_SHOTS, True, 2)
    await ctx.screenshot(label)
    shots = result.get("detail", [])
    held_ok = bool(shots) and all(s["heldS"] >= MIN_DRAW_S for s in shots)
    in_band = [s for s in shots if 600 <= s["range"] <= 1500]
    rate = (sum(1 for s in in_band if s["hit"]) / len(in_band)) if in_band else None
    far_band = [s for s in shots if 800 <= s["range"] <= 1500]
    far_rate = (sum(1 for s in far_band if s["hit"]) / len(far_band)) if far_band else None
    checks = {
        "ok": bool(result.get("ok")),
        "startedBlocked": not first.get("clear"),
        "repositioned": bool(result.get("repositions")),
        "reposition_expected_and_seen": (not expect_reposition) or bool(result.get("repositions")) or first.get("clear"),
        "fullDraws": held_ok,
        "hitRateAtRange": rate is not None and rate >= MIN_HIT_RATE,
        "targetDead": bool(result.get("targetDead")),
    }
    await kill(native, ref)
    return {"label": label, "pass": all(v for k, v in checks.items() if k not in ("startedBlocked", "repositioned")),
            "checks": checks, "hitRate600to1500": rate, "hitRate800to1500": far_rate, "shotsOver1500": sum(1 for s in shots if s["range"] > 1500), "shots": result.get("shots"), "hits": result.get("hits"),
            "ranges": [s["range"] for s in shots], "heldS": [s["heldS"] for s in shots], "repositions": result.get("repositions"),
            "model": result.get("model"), "detail": result.get("detail"), "error": result.get("error"), "suggestedPosition": result.get("suggestedPosition"),
            "firstLine": {k: first.get(k) for k in ("clear", "hitDistance", "hitLayer", "distance")}}


async def run(ctx):
    async def native(method, **params):
        return await ctx.call(method, **clean(params))

    details = {}
    state = await native("Player.state")
    pos = xyz(state["pos"])
    rotation = state.get("rotation")
    yaw = rotation[2] if isinstance(rotation, (list, tuple)) else (rotation or {}).get("z", 0.0)
    start = {"x": pos[0], "y": pos[1], "z": pos[2], "yawDeg": yaw}
    for bow in BOWS:
        try:
            await native("Player.addItem", editorID=bow, count=1)
            break
        except Exception as error:
            details["bowError"] = str(error)[:120]
    await native("Player.addItem", editorID=ARROWS, count=80)
    await native("Console.execute", command="tgm", target="player")

    # flat ground: 1000 units ahead of the start along +Y
    flat = await case(ctx, native, "flat-1000", (start["x"], start["y"] + 1000.0, start["z"]), False)
    details["flat"] = flat

    # bridge: the archer on the deck, the player under it
    await native("Player.teleport", **start)
    await asyncio.sleep(2)
    models = (await native("World.models", contains="bridge", radius=6000, limit=10)).get("refs", [])
    if not models:
        details["bridge"] = {"pass": False, "error": "no bridge model within 6000 units"}
    else:
        deck = xyz(models[0]["pos"])
        under = None
        for radius in (0, 150, 300):
            for k in range(8 if radius else 1):
                a = 2 * math.pi * k / 8
                x, y = deck[0] + radius * math.sin(a), deck[1] + radius * math.cos(a)
                await native("Player.teleport", x=x, y=y, z=deck[2] - 60.0, yawDeg=yaw)
                await asyncio.sleep(1.5)
                here = xyz((await native("Player.state"))["pos"])
                if here[2] < deck[2] - 40:
                    under = (x, y, here[2])
                    break
            if under:
                break
        if not under:
            details["bridge"] = {"pass": False, "error": "no spot below the deck found", "deck": deck}
        else:
            # the archer stands on the deck above the player; firing positions sampled around the player are 150-600 away
            above = (deck[0], deck[1], deck[2] + 40.0)
            details["bridge"] = await case(ctx, native, "bridge-above", above, True)
            details["bridge"]["deck"] = deck
            details["bridge"]["playerStart"] = under
    await native("Console.execute", command="tgm", target="player")
    return {"pass": all(details.get(k, {}).get("pass") for k in ("flat", "bridge")), "details": details}


class DryCtx:
    """A scripted stand-in for the game so the script's own logic runs without one (`--dry-run`)."""

    class Manager:
        def __init__(self, world):
            self.world = world

        async def call(self, method, timeout=None, **params):
            return await self.world.call(method, **params)

    def __init__(self):
        self.manager = DryCtx.Manager(self)
        self.health = 100.0
        self.shot = 0
        self.placed = False
        self.marks = []

    def mark(self, text):
        self.marks.append(text)

    async def screenshot(self, label):
        return label

    async def call(self, method, **p):
        if method == "Player.state":
            return {"pos": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0]}
        if method == "World.nearby":
            return {"refs": [{"refID": "00AA", "dead": False}] if self.placed else []}
        if method == "Console.execute":
            self.placed = self.placed or p["command"].startswith("placeatme")
            return {"lines": []}
        if method == "World.models":
            return {"refs": [{"pos": [500.0, 0.0, 300.0]}]}
        if method == "Ranged.loadout":
            return {"ready": True, "equipQueued": False, "bowEquipped": True, "ammoEquipped": True, "bow": {"speed": 1.0},
                    "ammo": {"projectile": {"speed": 3000, "gravity": 0.69}}}
        if method == "Ranged.state":
            return {"drawn": True, "position": [0, 0, 0], "ammoLeft": 50}
        if method == "Ranged.los":
            return {"clear": True, "from": [0, 0, 110], "torso": [0, 1000, 60], "head": [0, 1000, 120], "target": [0, 1000, 0],
                    "health": self.health, "dead": self.health <= 0, "distance": 1000}
        if method == "Combat.attack":
            self.health -= 40
            return {}
        if method == "Ranged.arrows":
            return {"arrows": []}
        return {}


if __name__ == "__main__":
    import json
    import sys

    if "--dry-run" in sys.argv:
        ranged.SETTLE_S = 0.0
        ranged.rm.default_draw_s = lambda speed: 0.2
        result = asyncio.run(run(DryCtx()))
        print(json.dumps(result, default=str))
        sys.exit(0 if result["details"].get("flat", {}).get("checks") else 1)
