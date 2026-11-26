"""Scenario start and the fast boring parts: scenario_setup, game_speed, fast_travel, wait.

`plan` turns a scenario spec (dict from JSON/YAML or a tool call) into ordered steps; `run_scenario` executes
them against a `call(method, **params)` coroutine and reports what was applied, what failed and what to be
careful about. The planner and the risk rules are pure so they are testable without a game.
"""

import asyncio
import json
from pathlib import Path

from .manager import GameError

# Armor and weapons are resolved by their displayed (English) names by the plugin.
KITS = {
    "light": [
        {"item": "Leather Armor", "equip": True}, {"item": "Leather Boots", "equip": True},
        {"item": "Leather Bracers", "equip": True}, {"item": "Leather Helmet", "equip": True},
        {"item": "Iron Dagger", "equip": True}, {"item": "Hunting Bow"}, {"item": "Iron Arrow", "count": 40},
        {"item": "Potion of Minor Healing", "count": 3}, {"item": "Lockpick", "count": 10}, {"item": "Gold", "count": 200},
    ],
    "medium": [
        {"item": "Steel Armor", "equip": True}, {"item": "Steel Boots", "equip": True},
        {"item": "Steel Gauntlets", "equip": True}, {"item": "Steel Helmet", "equip": True},
        {"item": "Steel Sword", "equip": True}, {"item": "Steel Shield", "equip": True},
        {"item": "Hunting Bow"}, {"item": "Iron Arrow", "count": 40},
        {"item": "Potion of Healing", "count": 5}, {"item": "Lockpick", "count": 10}, {"item": "Gold", "count": 500},
    ],
    "heavy": [
        {"item": "Dwarven Armor", "equip": True}, {"item": "Dwarven Boots", "equip": True},
        {"item": "Dwarven Gauntlets", "equip": True}, {"item": "Dwarven Helmet", "equip": True},
        {"item": "Dwarven War Axe", "equip": True}, {"item": "Dwarven Shield", "equip": True},
        {"item": "Hunting Bow"}, {"item": "Iron Arrow", "count": 40},
        {"item": "Potion of Plentiful Healing", "count": 5}, {"item": "Lockpick", "count": 10}, {"item": "Gold", "count": 1000},
    ],
}
KEYS = {"name", "quest", "stage", "completeStagesBefore", "stagePath", "position", "items", "kit", "level", "skills",
        "perks", "spells", "time", "weather", "clearBounty", "notes", "save"}


class ScenarioError(GameError):
    pass


def load_scenario(path):
    """Read a scenario file: JSON, or YAML when PyYAML is installed (it is a declared dependency)."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as error:
            raise ScenarioError("YAML scenarios need PyYAML (pip install pyyaml); JSON works without it") from error
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ScenarioError("a scenario file is one mapping")
    return data


def validate(spec):
    unknown = sorted(set(spec) - KEYS)
    if unknown:
        raise ScenarioError(f"unknown scenario field(s): {', '.join(unknown)}; known: {', '.join(sorted(KEYS))}")
    if spec.get("kit") is not None and spec["kit"] not in KITS:
        raise ScenarioError(f"kit is one of {', '.join(KITS)}")
    if spec.get("stage") is not None and not spec.get("quest"):
        raise ScenarioError("stage needs quest")
    position = spec.get("position")
    if position is not None:
        kinds = [k for k in ("ref", "cell", "questTarget", "place", "x") if k in position]
        if len(kinds) != 1 and not (kinds == ["x"] or set(kinds) == {"cell", "x"}):
            raise ScenarioError("position is one of {ref}, {cell, x?, y?, z?}, {x, y, z}, {place} or {questTarget}")
    level = spec.get("level")
    if level is not None and not (isinstance(level, int) and 1 <= level <= 81):
        raise ScenarioError("level is an integer from 1 to 81")


def item_list(spec):
    """Kit items first, then explicit items (an explicit entry for the same item replaces the kit's)."""
    entries = [dict(entry) for entry in KITS.get(spec.get("kit") or "", [])]
    for raw in spec.get("items") or []:
        entry = {"item": raw} if isinstance(raw, (str, int)) else dict(raw)
        entries = [e for e in entries if e["item"] != entry["item"]]
        entries.append(entry)
    return entries


def character_params(spec):
    params = {}
    items = item_list(spec)
    if items:
        params["items"] = items
    for key in ("level", "skills", "perks", "spells", "weather"):
        if spec.get(key) is not None:
            params[key] = spec[key]
    if spec.get("clearBounty"):
        params["clearBounty"] = True
    return params


def stage_path(spec, current):
    """Stages to run before the requested one. Stage lists are not readable from the engine, so without an
    explicit stagePath the multiples of 10 are a guess (the common authoring convention) and reported as such."""
    target = spec["stage"]
    if not spec.get("completeStagesBefore"):
        return [], False
    explicit = spec.get("stagePath")
    if explicit:
        return sorted(s for s in explicit if current < s < target), False
    return [s for s in range(10, target, 10) if s > current], True


def risk_warnings(spec, current_stage, running):
    warnings = []
    if not spec.get("quest") or spec.get("stage") is None:
        return warnings
    target = spec["stage"]
    if not spec.get("completeStagesBefore") and target > current_stage and (target > 10 or not running):
        warnings.append(
            f"jumping {spec['quest']} from stage {current_stage} straight to {target} skips the stages in between: "
            "their scripts (alias fills, objectives, enabled markers) never run, so targets may be missing or scripts "
            "may fire out of order. Set completeStagesBefore (with stagePath when the stage numbers are not every 10) "
            "and check quest_targets afterwards")
    if spec.get("completeStagesBefore") and not spec.get("stagePath"):
        warnings.append("completeStagesBefore without stagePath guesses the earlier stages as multiples of 10; "
                        "a stage number that does not exist is ignored by the game, a missing one is not run")
    if target < current_stage:
        warnings.append(f"stage {target} is behind the current stage {current_stage}; quests do not rewind cleanly, "
                        "load a save from before the quest instead")
    return warnings


def plan(spec):
    """Ordered step names the runner executes for this spec (used by tests and the dry run)."""
    validate(spec)
    steps = []
    if character_params(spec):
        steps.append("character")
    if spec.get("time") is not None:
        steps.append("time")
    if spec.get("quest") and spec.get("stage") is not None:
        steps.append("quest")
    if spec.get("position"):
        steps.append("position")
    if spec.get("quest"):
        steps.append("verify")
    return steps


async def wait_playable(call, timeout=90.0):
    """Block until a save is loaded and the load fade is gone; teleports across cells go through a loading screen."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    await asyncio.sleep(0.4)
    while loop.time() < deadline:
        try:
            snap = await call("Events.snapshot")
            names = {m["name"] for m in snap.get("menus", [])}
            if snap.get("playable") and "Fader Menu" not in names and "Loading Menu" not in names:
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


def pick_target(targets, quest, exact):
    """The reference to stand at for a quest target: the way to it (a load door) unless the exact target is asked for."""
    for entry in targets.get("targets", []):
        side = entry.get("target") if exact else (entry.get("tracking") or entry.get("target"))
        if side and side.get("refID"):
            return side, entry
    raise ScenarioError(f"{quest} shows no quest target with a reference now; pick a position another way")


async def apply_position(call, position):
    if "ref" in position:
        result = await call("Player.teleport", refID=position["ref"])
    elif "place" in position:
        places = await call("Nav.places", contains=position["place"], limit=5)
        match = next((p for p in places.get("places", []) if p["name"] == position["place"]), None) or (
            places["places"][0] if places.get("places") else None)
        if not match:
            raise ScenarioError(f"no map-marker place named {position['place']!r}; see nav_places")
        result = await call("Player.teleport", refID=match["marker"])
    elif "questTarget" in position:
        targets = await call("Quest.targets", quest=position["questTarget"], limit=5)
        side, entry = pick_target(targets, position["questTarget"], position.get("exact", False))
        result = await call("Player.teleport", refID=side["refID"])
        result["questTarget"] = {"refID": side["refID"], "name": side.get("name"), "objective": entry.get("text")}
    else:
        if "cell" in position:
            await call("World.centerOnCell", cell=position["cell"])
            await wait_playable(call)
        if "x" in position:
            result = await call("Player.teleport", x=position["x"], y=position["y"], z=position["z"],
                                **({"yawDeg": position["yaw"]} if "yaw" in position else {}))
        else:
            result = {"cell": position["cell"]}
    await wait_playable(call)
    return result


async def run_scenario(call, spec, settle=0.5):
    """Apply a scenario; never raises for a failed step, so the report says what state the game was left in."""
    validate(spec)
    applied, failures, warnings = [], [], []
    report = {"name": spec.get("name"), "applied": applied, "failures": failures, "warnings": warnings}

    async def attempt(step, coroutine):
        try:
            result = await coroutine
        except Exception as error:
            failures.append({"step": step, "error": str(error)})
            return None
        applied.append({"step": step, **({"result": result} if result is not None else {})})
        return result

    params = character_params(spec)
    if params:
        result = await attempt("character", call("Scenario.character", timeout=60, **params))
        for step in (result or {}).get("steps", []):
            if not step.get("ok"):
                failures.append({"step": step["step"], "error": step.get("reason"), "detail": {
                    k: v for k, v in step.items() if k not in ("step", "ok", "reason")}})
    if spec.get("time") is not None:
        hour = float(spec["time"])
        if not 0 <= hour < 24:
            failures.append({"step": "time", "error": "time is an hour from 0 to 24"})
        else:
            await attempt("time", call("Console.execute", command=f"set gamehour to {hour}", target="none"))
    if spec.get("quest") and spec.get("stage") is not None:
        quest = spec["quest"]
        before = {}
        try:
            before = await call("Quest.get", editorID=quest) if not quest[:2].isdigit() else await call("Quest.get", formID=quest)
        except Exception as error:
            failures.append({"step": "quest", "error": f"cannot read quest {quest}: {error}"})
        if before:
            current = before.get("stage") or 0
            warnings.extend(risk_warnings(spec, current, before.get("running")))
            path, guessed = stage_path(spec, current)
            for stage in path + [spec["stage"]]:
                result = await attempt(f"stage {stage}", call("Scenario.setStage", quest=quest, stage=stage))
                await asyncio.sleep(settle)
                if result and result.get("after") != stage and stage == spec["stage"]:
                    failures.append({"step": f"stage {stage}", "error": f"the quest is at stage {result.get('after')} "
                                     f"after setstage; a stage script may have moved it on, or the stage does not exist"})
            if guessed and path:
                applied.append({"step": "guessed stages", "result": path})
    if spec.get("position"):
        result = await attempt("position", apply_position(call, spec["position"]))
        if result is None:
            warnings.append("the player was not moved; the character and quest steps above were still applied")
    if spec.get("quest"):
        try:
            targets = await call("Quest.targets", quest=spec["quest"], limit=5)
            report["questTargets"] = [{k: t.get(k) for k in ("text", "target", "tracking", "distance")}
                                      for t in targets.get("targets", targets if isinstance(targets, list) else [])][:5]
            if spec.get("stage") is not None and not report["questTargets"]:
                warnings.append("the quest shows no marker at this stage; the stage may have been entered without its "
                                "objective scripts (see completeStagesBefore)")
        except Exception as error:
            warnings.append(f"could not read quest targets: {error}")
    try:
        state = await call("Player.state")
        report["player"] = {"cell": state.get("cell"), "location": state.get("location"), "pos": state.get("pos"),
                            "level": state.get("level")}
    except Exception:
        pass
    report["ok"] = not failures
    return report


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def register(mcp, manager, native):
    @mcp.tool()
    async def scenario_setup(
        quest: str | None = None, stage: int | None = None, complete_stages_before: bool = False,
        stage_path: list[int] | None = None, position: dict | None = None, items: list | None = None,
        kit: str | None = None, level: int | None = None, skills: dict | None = None, perks: list | None = None,
        spells: list | None = None, time: float | None = None, weather: str | None = None,
        clear_bounty: bool = False, file: str | None = None,
    ) -> str:
        """Set up a test at any point instead of walking from the start. One call applies, in order: character (kit
        light/medium/heavy plus items, level, skills {"OneHanded": 60}, perks, spells, weather, clear_bounty), game
        hour, quest stage (quest = editor id, form id; stage = the stage to jump to) and position. position is one of
        {"ref": "<refID>"}, {"cell": "<editor id>", "x":..,"y":..,"z":..}, {"place": "<map marker name>"} or
        {"questTarget": "<quest>"} (stands at the way to the quest marker, e.g. the dungeon door; "exact": true for
        the marker's own reference). Items are names ("Steel Sword") or form ids, as strings or
        {"item", "count", "equip"}. Quests that need earlier stages: complete_stages_before runs the stages before
        `stage` in order (stage_path lists them; without it multiples of 10 are tried, which is a guess); jumping
        without it warns that scripts of the skipped stages never ran. `file` loads a JSON/YAML scenario file on the
        host running this server and the other arguments override it. Returns {ok, applied, failures, warnings,
        questTargets, player}; read `failures` and `warnings` before trusting the setup. Needs a playable world."""
        spec = load_scenario(file) if file else {}
        given = {"quest": quest, "stage": stage, "completeStagesBefore": complete_stages_before or None, "stagePath": stage_path,
                 "position": position, "items": items, "kit": kit, "level": level, "skills": skills, "perks": perks,
                 "spells": spells, "time": time, "weather": weather, "clearBounty": clear_bounty or None}
        spec.update({k: v for k, v in given.items() if v is not None})
        report = await run_scenario(lambda method, **params: native(method, **params), spec)
        return dumps(report)

    @mcp.tool()
    async def game_speed(walk: float | None = None, timescale: float | None = None, reset: bool = False,
                         force: bool = False) -> str:
        """Speed up the boring parts. walk = player walk speed in percent of normal (100 = normal, 25-400; force
        lifts the cap to 1000, where physics and navigation get unreliable); timescale = game minutes per real minute
        (default 20); reset = back to normal walk speed. With no arguments, reads the current values. nav_goto
        {fast: true} does the walk part automatically and restores it on arrival, combat and dialogue."""
        if walk is None and timescale is None and not reset:
            return dumps(await native("Game.speed"))
        return dumps(await native("Game.setSpeed", walk=walk, timescale=timescale, reset=True if reset else None,
                                  force=True if force else None))

    @mcp.tool()
    async def fast_travel(to: str, force: bool = False, skip_time: bool = True) -> str:
        """Fast travel to a map marker or location by displayed name ("Whiterun") or form id (a marker reference).
        Only markers discovered on the map are accepted unless force=true. The map UI is not driven: the player is
        moved to the marker and the game clock advances by the estimated travel time (skip_time=false keeps it).
        Waits for the loading screen to end."""
        result = await native("Game.fastTravel", to=to, force=True if force else None, skipTime=skip_time)
        result["playable"] = await wait_playable(lambda method, **params: native(method, **params))
        return dumps(result)

    @mcp.tool()
    async def wait(hours: float) -> str:
        """Skip game time (0-720 hours), as the wait menu does: the clock advances at once and the sky follows.
        Timers and NPC schedules catch up on their next update; it is not a simulation of the hours in between."""
        return dumps(await native("Game.waitHours", hours=hours))
