"""Live check of the introspection methods on the Riverwood fixture (shared session, `run(ctx)`).

One game launch covers: a Scaleform find/get/set/invoke round trip in the Journal, Papyrus classes and calls
(a global, a member, a latent one with and without enough time), form search and metadata, idles on a Riverwood
NPC, the free camera and the orbit camera switch, and the custom waypoint through the open map. Every step
records its measured value; the case passes when all checks hold. `--dry-run` runs the logic against a fake.
"""
import asyncio
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PREFERRED_IDLES = ("IdleWave", "IdleSalute", "IdleStudy", "IdleLaugh", "IdleCivilWarCheer")


def clean(params):
    return {k: v for k, v in params.items() if v is not None}


def xyz(pos):
    return tuple(pos) if isinstance(pos, (list, tuple)) else (pos["x"], pos["y"], pos["z"])


def near(a, b, tolerance):
    return a is not None and b is not None and abs(a - b) <= tolerance


class Checks:
    def __init__(self):
        self.details = {}
        self.failed = []

    def check(self, name, ok, **measured):
        self.details[name] = {"ok": bool(ok), **measured}
        if not ok:
            self.failed.append(name)
        return bool(ok)

    async def attempt(self, name, make, judge):
        """Run one step; an RPC error is a failed check with the error text, not an abort."""
        try:
            result = await make()
        except Exception as error:  # noqa: BLE001 - any failure of a step is recorded
            self.check(name, False, error=str(error)[:300])
            return None
        try:
            ok, measured = judge(result)
        except Exception as error:  # noqa: BLE001
            self.check(name, False, error=f"unexpected reply: {error!r}", reply=str(result)[:300])
            return result
        self.check(name, ok, **measured)
        return result


async def scaleform(ctx, native, checks):
    await native("UI.openMenu", name="Journal Menu", wait=True)
    await asyncio.sleep(1.5)
    menu = "Journal Menu"
    dump = await checks.attempt("gfx.dump", lambda: native("UI.dump", menu=menu, depth=1),
                                lambda r: (isinstance(r.get("value"), dict) and len(r["value"]) > 0, {"members": len(r["value"])}))
    found = None
    for word in ("quests", "skills", "system", "stats"):
        try:
            reply = await native("UI.find", menu=menu, text=word, depth=8, limit=5)
        except Exception as error:  # noqa: BLE001
            checks.check("gfx.find", False, error=str(error)[:300])
            break
        if reply["hits"]:
            found = (word, reply["hits"][0])
            break
    if found:
        word, hit = found
        value = await native("UI.getVariable", menu=menu, path=hit["path"], depth=1)
        checks.check("gfx.find", True, text=word, path=hit["path"], read=str(value.get("value"))[:80])
    elif dump is not None:
        checks.check("gfx.find", False, note="no journal label found for quests/skills/system/stats")
    await checks.attempt("gfx.set_get", lambda: native("UI.setVariable", menu=menu, path="_root.__probe", value=42),
                         lambda r: (r["value"] == 42, {"value": r["value"]}))
    await native("UI.setVariable", menu=menu, path="_root.__probeArr", value=[10, 20, 30])
    await checks.attempt("gfx.array_index", lambda: native("UI.getVariable", menu=menu, path="_root.__probeArr.1"),
                         lambda r: (r["value"] == 20, {"value": r["value"]}))
    await checks.attempt("gfx.global_invoke", lambda: native("UI.invoke", menu=menu, path="_global.Math.max", args=[3, 7]),
                         lambda r: (r["result"] == 7, {"result": r["result"]}))
    await native("UI.closeMenu", name="Journal Menu")
    await asyncio.sleep(0.5)


async def papyrus(ctx, native, checks, player):
    await checks.attempt("papyrus.classes", lambda: native("Papyrus.classes", filter="game", limit=20),
                         lambda r: (any(c.lower() == "game" for c in r["classes"]), {"total": r["total"]}))
    await checks.attempt("papyrus.classes_paging", lambda: native("Papyrus.classes", limit=3, offset=2),
                         lambda r: (len(r["classes"]) == 3 and r["total"] > 100, {"total": r["total"], "page": r["classes"]}))
    await checks.attempt("papyrus.functions", lambda: native("Papyrus.functions", **{"class": "Game"}, kind="global", filter="getplayer"),
                         lambda r: (any(f["name"] == "GetPlayer" for f in r["functions"]), {"found": [f["name"] for f in r["functions"]]}))
    await checks.attempt("papyrus.global_getplayer", lambda: native("Papyrus.call", **{"class": "Game"}, function="GetPlayer"),
                         lambda r: (str((r["value"] or {}).get("formID", "")).upper().endswith("14"), {"value": r["value"]}))
    await checks.attempt("papyrus.global_gametime", lambda: native("Papyrus.call", **{"class": "Utility"}, function="GetCurrentGameTime"),
                         lambda r: (isinstance(r["value"], (int, float)) and r["value"] > 0, {"value": r["value"]}))
    await checks.attempt("papyrus.member_posx", lambda: native("Papyrus.call", form="0x14", function="GetPositionX"),
                         lambda r: (near(r["value"], player[0], 5), {"value": r["value"], "playerX": player[0]}))
    await checks.attempt("papyrus.member_form_arg",
                         lambda: native("Papyrus.call", form="0x14", **{"class": "ObjectReference"}, function="GetDistance", args=["0x14"]),
                         lambda r: (near(r["value"], 0, 1), {"value": r["value"]}))
    began = asyncio.get_running_loop().time()
    await checks.attempt("papyrus.latent_ok", lambda: native("Papyrus.call", **{"class": "Utility"}, function="Wait", args=[0.6], timeoutMs=8000),
                         lambda r: (asyncio.get_running_loop().time() - began >= 0.4, {"waitedS": round(asyncio.get_running_loop().time() - began, 2), "value": r["value"]}))
    try:
        await native("Papyrus.call", **{"class": "Utility"}, function="Wait", args=[5.0], timeoutMs=300)
        checks.check("papyrus.latent_timeout", False, note="returned instead of timing out")
    except Exception as error:  # noqa: BLE001
        checks.check("papyrus.latent_timeout", getattr(error, "code", None) == -32008, error=str(error)[:200])
    try:
        await native("Papyrus.call", **{"class": "Game"}, function="GetPlayer", args=[1])
        checks.check("papyrus.bad_arity", False, note="accepted a wrong argument count")
    except Exception as error:  # noqa: BLE001
        checks.check("papyrus.bad_arity", getattr(error, "code", None) == -32602, error=str(error)[:200])


async def forms(ctx, native, checks):
    sword = await checks.attempt("forms.find_weapon", lambda: native("Forms.find", type="Weapon", name="iron sword", limit=5),
                                 lambda r: (r["total"] >= 1 and r["forms"][0]["type"] == "Weapon" and bool(r["forms"][0].get("plugin")),
                                            {"total": r["total"], "first": r["forms"][0] if r["forms"] else None}))
    if sword and sword["forms"]:
        await checks.attempt("forms.info", lambda: native("Forms.info", formID=sword["forms"][0]["formID"]),
                             lambda r: (bool(r["plugins"]) and len(r["keywords"]) > 0, {"plugins": r["plugins"], "keywords": len(r["keywords"])}))
    page1 = await native("Forms.find", type="Keyword", plugin="skyrim.esm", limit=3)
    page2 = await native("Forms.find", type="Keyword", plugin="skyrim.esm", limit=3, offset=3)
    ids1 = [f["formID"] for f in page1["forms"]]
    ids2 = [f["formID"] for f in page2["forms"]]
    checks.check("forms.paging", len(ids1) == 3 and len(ids2) == 3 and not set(ids1) & set(ids2) and ids1 == sorted(ids1),
                 total=page1["total"], page1=ids1, page2=ids2)
    try:
        await native("Forms.find", type="Nonsense", name="x")
        checks.check("forms.unknown_type", False, note="accepted an unknown type")
    except Exception as error:  # noqa: BLE001
        checks.check("forms.unknown_type", getattr(error, "code", None) == -32602, error=str(error)[:200])


async def idles(ctx, native, checks):
    nearby = (await native("World.nearby", radius=3500, types=["actor"], limit=40))["refs"]
    npcs = [r for r in nearby if not r.get("dead") and r.get("loaded", True) and not r.get("hostile")]
    if not npcs:
        checks.check("idles.npc", False, note="no living NPC within 3500 units")
        return
    npc = npcs[0]
    checks.check("idles.npc", True, ref=npc["refID"], npc=npc.get("name"))
    listing = await checks.attempt("idles.list", lambda: native("Idles.list", ref=npc["refID"], limit=300),
                                   lambda r: (r["total"] > 0, {"total": r["total"], "conditionsChecked": r["conditionsChecked"]}))
    if not listing or not listing["idles"]:
        return
    by_id = {i["editorID"]: i for i in listing["idles"] if i.get("editorID")}
    pick = next((by_id[n] for n in PREFERRED_IDLES if n in by_id), listing["idles"][0])
    played = await checks.attempt("idles.play", lambda: native("Idles.play", ref=npc["refID"], idle=pick["formID"]),
                                  lambda r: ("accepted" in r, {"idle": pick.get("editorID") or pick["formID"], "accepted": r.get("accepted")}))
    await asyncio.sleep(1.0)
    await checks.attempt("idles.stop", lambda: native("Idles.stop", ref=npc["refID"]), lambda r: (r["sent"] is True, {"sent": r["sent"]}))
    return played


async def camera(ctx, native, checks, player):
    await checks.attempt("camera.pose", lambda: native("Camera.pose"),
                         lambda r: (r["pov"] in ("first", "third") and r["free"] is False and "pos" in r, {"mode": r["mode"], "pov": r["pov"]}))
    await checks.attempt("camera.auto_vanity_off", lambda: native("Camera.autoVanity", allow=False),
                         lambda r: (r["allow"] is False, {"allow": r["allow"]}))
    target = {"pos": [player[0], player[1], player[2] + 400.0], "yawDeg": 90.0, "pitchDeg": 25.0}
    first = await native("Camera.free", on=True, **target)
    if first.get("pending"):
        await asyncio.sleep(0.3)
        await native("Camera.free", on=True, **target)
    await asyncio.sleep(0.5)

    def judge(state):
        pos = state.get("pos") or [None] * 3
        return (state["mode"] == "free" and near(state.get("yawDeg"), 90.0, 5) and near(state.get("pitchDeg"), 25.0, 5)
                and all(near(pos[i], target["pos"][i], 5) for i in range(3)),
                {"mode": state["mode"], "pos": state.get("pos"), "yawDeg": state.get("yawDeg"), "pitchDeg": state.get("pitchDeg"),
                 "firstReply": first})

    await checks.attempt("camera.free_pose", lambda: native("Camera.state"), judge)
    await native("Camera.free", on=False)
    await asyncio.sleep(0.5)
    await checks.attempt("camera.free_off", lambda: native("Camera.pose"), lambda r: (r["free"] is False and r["pov"] in ("first", "third"), {"mode": r["mode"]}))
    await native("Camera.mode", mode="vanity")
    await asyncio.sleep(0.5)
    await checks.attempt("camera.vanity", lambda: native("Camera.state"), lambda r: (r["mode"] == "autoVanity", {"mode": r["mode"]}))
    await native("Camera.mode", mode="third")
    await asyncio.sleep(0.5)
    await checks.attempt("camera.third", lambda: native("Camera.pose"), lambda r: (r["pov"] == "third", {"mode": r["mode"]}))
    await native("Camera.mode", mode="first")
    await asyncio.sleep(0.5)
    await checks.attempt("camera.first", lambda: native("Camera.pose"), lambda r: (r["pov"] == "first", {"mode": r["mode"]}))
    await native("Camera.mode", mode="third")
    await native("Camera.autoVanity", allow=True)


async def waypoint(ctx, native, checks, player):
    await native("Map.clearWaypoint")
    await checks.attempt("map.waypoint_empty", lambda: native("Map.getWaypoint"), lambda r: (r["set"] is False, {"set": r["set"]}))
    await native("UI.openMenu", name="MapMenu", wait=True)
    await asyncio.sleep(3.0)
    goal = (player[0] + 600.0, player[1] + 600.0, player[2])
    placed = await checks.attempt("map.set_waypoint", lambda: native("Map.setWaypoint", x=goal[0], y=goal[1], z=goal[2]),
                                  lambda r: (True, {"reply": r}))
    await asyncio.sleep(1.0)
    await native("UI.closeMenu", name="MapMenu")
    await asyncio.sleep(1.0)
    await checks.attempt("map.read_waypoint", lambda: native("Map.getWaypoint"),
                         lambda r: (r["set"] is True and near(r["pos"][0], goal[0], 150) and near(r["pos"][1], goal[1], 150),
                                    {"set": r["set"], "pos": r.get("pos"), "requested": goal}))
    await checks.attempt("map.clear_waypoint", lambda: native("Map.clearWaypoint"), lambda r: (r["set"] is False, {"cleared": r.get("cleared")}))
    markers = await checks.attempt("map.markers", lambda: native("Map.markers", contains="Riverwood", limit=5),
                                   lambda r: (r["total"] >= 1 and "visible" in r["markers"][0], {"total": r["total"], "first": r["markers"][0] if r["markers"] else None}))
    return placed, markers


async def run(ctx):
    async def native(method, **params):
        return await ctx.call(method, **clean(params))

    checks = Checks()
    state = await native("Player.state")
    player = xyz(state["pos"])
    steps = (("scaleform", lambda: scaleform(ctx, native, checks)), ("papyrus", lambda: papyrus(ctx, native, checks, player)),
             ("forms", lambda: forms(ctx, native, checks)), ("idles", lambda: idles(ctx, native, checks)),
             ("camera", lambda: camera(ctx, native, checks, player)), ("waypoint", lambda: waypoint(ctx, native, checks, player)))
    for name, make in steps:
        ctx.mark(f"introspect {name}")
        try:
            await make()
        except Exception as error:  # noqa: BLE001 - a broken group must not hide the others
            checks.check(f"{name}.group", False, error=str(error)[:300])
        finally:
            # leave the world playable for whatever runs next
            for cleanup in (("UI.closeMenu", {"name": "Journal Menu"}), ("UI.closeMenu", {"name": "MapMenu"}), ("Camera.free", {"on": False})):
                try:
                    await native(cleanup[0], **cleanup[1])
                except Exception:  # noqa: BLE001
                    pass
    return {"pass": not checks.failed, "details": {"failed": checks.failed, **checks.details}}


class DryCtx:
    """A scripted stand-in for the game so the script's own logic runs without one (`--dry-run`)."""

    def __init__(self):
        self.waypoint = None
        self.camera = {"mode": "thirdPerson", "pos": [0, 0, 0], "yawDeg": 0, "pitchDeg": 0}
        self.vars = {}

    def mark(self, text):
        pass

    async def call(self, method, timeout=None, **p):
        if method == "Player.state":
            return {"pos": [100.0, 200.0, 300.0]}
        if method == "UI.dump":
            return {"value": {"a": 1}}
        if method == "UI.find":
            return {"hits": [{"path": "_root.Tab.text"}]}
        if method == "UI.getVariable":
            path = p["path"]
            if path.endswith(".1"):
                return {"value": self.vars.get(path.rsplit(".", 1)[0], [0, 0])[1]}
            return {"value": self.vars.get(path, "QUESTS")}
        if method == "UI.setVariable":
            self.vars[p["path"]] = p["value"]
            return {"value": p["value"]}
        if method == "UI.invoke":
            return {"result": max(p["args"])}
        if method == "Papyrus.classes":
            return {"total": 900, "classes": ["Game", "Actor", "Alias"][: p.get("limit", 3)]}
        if method == "Papyrus.functions":
            return {"functions": [{"name": "GetPlayer"}]}
        if method == "Papyrus.call":
            fn = p["function"]
            if fn == "GetPlayer" and p.get("args"):
                from skyrim_devtools.client import RpcError
                raise RpcError(-32602, "takes 0 argument(s)")
            if fn == "Wait" and p["timeoutMs"] == 300:
                from skyrim_devtools.client import RpcError
                raise RpcError(-32008, "timeout")
            if fn == "Wait":
                await asyncio.sleep(0.5)
                return {"value": None}
            return {"value": {"GetPlayer": {"formID": "00000014"}, "GetCurrentGameTime": 12.5, "GetPositionX": 100.0, "GetDistance": 0.0}[fn]}
        if method == "Forms.find":
            if p.get("type") == "Nonsense":
                from skyrim_devtools.client import RpcError
                raise RpcError(-32602, "type is a form type name")
            if p.get("type") == "Keyword":
                base = p.get("offset", 0)
                return {"total": 900, "forms": [{"formID": f"{0x100 + base + i:08X}"} for i in range(3)]}
            return {"total": 1, "forms": [{"formID": "00012EB7", "type": "Weapon", "plugin": "Skyrim.esm"}]}
        if method == "Forms.info":
            return {"plugins": ["Skyrim.esm"], "keywords": [{}, {}]}
        if method == "World.nearby":
            return {"refs": [{"refID": "000A1", "name": "Hod"}]}
        if method == "Idles.list":
            return {"total": 2, "conditionsChecked": True, "idles": [{"formID": "0001", "editorID": "IdleWave"}]}
        if method == "Idles.play":
            return {"accepted": True}
        if method == "Idles.stop":
            return {"sent": True}
        if method == "Camera.pose":
            return {"pov": "first" if self.camera["mode"] == "firstPerson" else "third", "free": self.camera["mode"] == "free", "pos": [0, 0, 0], "mode": self.camera["mode"]}
        if method == "Camera.autoVanity":
            return {"allow": p["allow"]}
        if method == "Camera.free":
            if p["on"]:
                self.camera = {"mode": "free", "pos": p["pos"], "yawDeg": p["yawDeg"], "pitchDeg": p["pitchDeg"]}
            else:
                self.camera["mode"] = "thirdPerson"
            return {"applied": True}
        if method == "Camera.state":
            return self.camera
        if method == "Camera.mode":
            self.camera["mode"] = {"vanity": "autoVanity", "third": "thirdPerson", "first": "firstPerson"}[p["mode"]]
            return {}
        if method == "Map.clearWaypoint":
            self.waypoint = None
            return {"set": False}
        if method == "Map.getWaypoint":
            return {"set": True, "pos": list(self.waypoint)} if self.waypoint else {"set": False}
        if method == "Map.setWaypoint":
            self.waypoint = (p["x"], p["y"], p["z"])
            return {}
        if method == "Map.markers":
            return {"total": 1, "markers": [{"visible": True}]}
        return {}


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        result = asyncio.run(run(DryCtx()))
        print(json.dumps(result, default=str))
        sys.exit(0 if result["pass"] else 1)
