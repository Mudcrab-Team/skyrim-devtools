"""Exploration tools for dungeons and mines with no quest marker (SPDX-License-Identifier: GPL-3.0-or-later).

`cell_map` summarises the current cell into rooms (see skyrim_devtools/rooms.py) with what lies in each and which
have been visited. `explore` picks the next room or reference for a goal, walks there with Nav.goto, optionally
fights what it meets with `combat_fight`, and says when the cell is cleared or fully explored. `choose_target`
is the whole policy and is a pure function of the map model, so it is tested without a game.
"""

import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .. import rooms as rooms_module

NAV_LEG_TIMEOUT_S = 150.0
FIGHTS_PER_LEG = 6
GIVE_UP_AFTER = 2          # failed legs to the same target before it is skipped
STUCK_AFTER = 4            # consecutive failed legs before explore stops
HIT_WINDOW_S = 4.5         # Nav.goto counts a hit taken this recently as combat
ARRIVE_NEAR = 220.0        # already this close to a reference target: count it as reached
LEVER_REACH = 2500.0       # levers this far from where a way ended are worth walking to
PULLS_PER_CELL = 6
PULLS_PER_LEVER = 2        # a second pull only when the first one left the blocker as it was
UNLOCK_UP_TO = 2           # lock ranks (veryEasy=1, easy=2) explore opens itself when a hostile sits behind one
UNLOCK_REACH = 1800.0      # locked doors this close to an unreachable hostile are tried
UNLOCKS_PER_CELL = 6
OPEN_STATES = ("open", "opening")
SETTLE_S = 5.0             # gates and drawbridges animate for a few seconds after a pull
LOOSE_WORDS = re.compile(r"torch|candle|lantern|lamp|brazier|sconce|chandelier|clutter|light", re.I)
BLOCKED_REASON = re.compile(r"^(Dead end|stuck)", re.I)
FORM_ID = re.compile(r"\b[0-9A-Fa-f]{8}\b")


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def quest_hint(reply):
    """Extra fields for a Quest.targets reply: when no objective points anywhere in reach of this cell, say so and
    suggest explore. Returns {} when a marker is usable."""
    targets = (reply or {}).get("targets") or []
    usable = []
    for entry in targets:
        for key in ("tracking", "target"):
            ref = entry.get(key)
            if ref and ref.get("sameSpace"):
                usable.append(ref)
                break
    if usable:
        return {}
    if not targets:
        return {"inThisCell": False,
                "hint": "no quest marker is showing, so no objective has a target in this cell: use explore (goal clear, "
                        "exit, loot or find:<name>) to look for it, and read quest_active for the objective text"}
    return {"inThisCell": False,
            "hint": "every objective target is in another cell and no door on this side is marked: use explore "
                    "goal exit to leave, or explore to look for the way"}


# ---- policy --------------------------------------------------------------------------------------------------

class Memory:
    """What explore has done in one cell this session."""

    def __init__(self):
        self.reached = set()      # room ids
        self.approached = set()   # reference ids walked up to
        self.failures = Counter() # target key -> failed legs
        self.blocked = set()      # target keys given up on
        self.killed = []          # names of hostiles seen to be gone
        self.pulled = set()       # lever-like activators already operated
        self.levers = {}          # lever ref -> {"pulls", "blocker", "before", "after", "changed"} from its last pull
        self.unlocked = set()     # doors explore unlocked
        self.unreachable = {}     # target key -> why it was given up on


def _ranked(model, candidates, key_room):
    """Candidates sorted by walking distance over the room graph from the player's room, then by straight distance."""
    here = model.player_room
    path = model.room_path(here) if here else {}
    player = model.player or (0, 0, 0)

    def score(item):
        room = key_room(item)
        # rooms the navmesh does not join to ours (doors, gates, streamed sections) still get a try, after the joined ones
        far = path.get(room, 1e8) if here else 0.0
        pos = item.get("pos") if isinstance(item, dict) else None
        if pos is None and room in model.rooms:
            pos = model.rooms[room].anchor
        return far, rooms_module.dist3(pos, player) if pos else 1e9

    unjoined = sum(1 for c in candidates if here and key_room(c) not in path)
    return sorted(candidates, key=score), unjoined


def repull_allowed(memory, ref):
    """A lever toggles, so pulling it again only makes sense when its last pull left the blocker unchanged."""
    entry = memory.levers.get(ref)
    return bool(entry) and entry["changed"] is False and entry["pulls"] < PULLS_PER_LEVER


def lever_candidates(model, near, memory):
    """Levers, chains and switches worth pulling when a way ahead is shut: every untried one first, nearest to
    `near` first, then levers whose earlier pull changed nothing.

    A torch or other loose fixture is an activator too, but taking it opens nothing, so it is never a candidate.
    Which lever belongs to which gate is not known from the map, so distance to the place the way ended decides."""
    fresh, again = [], []
    for slot in model.contents.values():
        for a in slot["activators"]:
            name = a.get("name") or ""
            if not rooms_module.LEVER_WORDS.search(name) or LOOSE_WORDS.search(name):
                continue
            tried = a["refID"] in memory.pulled
            if tried and not repull_allowed(memory, a["refID"]):
                continue
            pos = a.get("pos")
            distance = rooms_module.dist3(pos, near) if pos and near else 0.0
            if distance <= LEVER_REACH:
                (again if tried else fresh).append((distance, a))
    return [a for _, a in sorted(fresh, key=lambda item: item[0]) + sorted(again, key=lambda item: item[0])]


def blocker_ref(blockers):
    """The reference a dead end names as the likely cause (a gate, bridge or door), else the nearest shut one."""
    if not blockers:
        return None
    match = FORM_ID.search(str(blockers.get("likely") or ""))
    if match:
        return match.group(0).upper()
    for entry in blockers.get("refs") or []:
        if entry.get("openState") not in OPEN_STATES and entry.get("refID"):
            return entry["refID"]
    return None


def record_pull(memory, lever, blocker, before, after):
    """Remember what a pull did to the blocker. changed is None when the blocker's state could not be read."""
    entry = memory.levers.get(lever, {"pulls": 0})
    known = blocker is not None and before is not None and after is not None
    memory.levers[lever] = {"pulls": entry["pulls"] + 1, "blocker": blocker, "before": before, "after": after,
                            "changed": (before != after) if known else None}
    memory.pulled.add(lever)
    return memory.levers[lever]


def enemy_ref(result):
    """The hostile that interrupted a walk, from Nav.status interruptions."""
    recent = ((result.get("interruptions") or {}).get("recent")) or []
    for event in reversed(recent):
        enemy = (event.get("details") or {}).get("enemy") or {}
        if enemy.get("refID"):
            return enemy["refID"]
    return None


def door_candidates(model, near, memory, max_rank=UNLOCK_UP_TO):
    """Locked doors near `near` that are easy enough to open (veryEasy, easy), nearest first."""
    found = []
    for slot in model.contents.values():
        for d in slot["doors"]:
            rank = rooms_module.LOCK_RANK.get(d.get("lockLevel"), 99)
            if not d.get("locked") or d["refID"] in memory.unlocked or d.get("teleports") or rank > max_rank:
                continue
            pos = d.get("pos")
            distance = rooms_module.dist3(pos, near) if pos and near else 0.0
            if distance <= UNLOCK_REACH:
                found.append((distance, d))
    return [d for _, d in sorted(found, key=lambda item: item[0])]


class StepLog:
    """One line per decision, kept in memory and appended to a file as it happens so a crash or timeout loses nothing."""

    def __init__(self, path=None):
        self.path = path
        self.lines = []
        self.began = time.monotonic()

    def __call__(self, text):
        line = f"{time.monotonic() - self.began:7.1f}s {text}"
        self.lines.append(line)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self.path = None


def blocked_by_gate(result):
    """A leg failed because the loaded navmeshes end at something shut, not because of a fight or a timeout."""
    return result.get("state") == "failed" and bool(BLOCKED_REASON.search(str(result.get("reason") or "")))


def ref_target(entry, kind, why):
    return {"status": "go", "kind": "ref", "kindOf": kind, "ref": entry["refID"], "name": entry.get("name"),
            "room": entry.get("room"), "pos": entry.get("pos"), "why": why, "key": "ref:" + entry["refID"]}


def room_target(model, rid, why):
    room = model.rooms[rid]
    return {"status": "go", "kind": "room", "room": rid, "pos": list(room.anchor), "why": why, "key": "room:" + rid}


def choose_target(model, goal, memory):
    """The next thing to walk to for `goal`, or a final status: {"status": "cleared"|"fully_explored"|"found"|...}.

    goal is "unvisited" (the nearest unvisited room), "clear", "loot", "exit" or "find:<name>"."""
    goal = (goal or "unvisited").strip()
    player = model.player or (0, 0, 0)
    skip = memory.blocked

    def near(entry):
        return entry.get("pos") and rooms_module.dist3(entry["pos"], player) <= ARRIVE_NEAR

    def unvisited_room():
        todo = [rid for rid in model.unvisited() if "room:" + rid not in skip]
        ranked, unreachable = _ranked(model, todo, lambda rid: rid)
        if ranked:
            return room_target(model, ranked[0], "nearest unvisited room"), unreachable
        return None, unreachable

    if goal == "clear":
        alive = model.hostiles()
        hostiles = [h for h in alive if "ref:" + h["refID"] not in skip]
        if alive and not hostiles:
            return {"status": "stuck", "why": f"{len(alive)} hostile(s) left that could not be reached or killed",
                    "hostiles": [{"refID": h["refID"], "name": h.get("name"), "room": h.get("room")} for h in alive]}
        if not hostiles:
            return {"status": "cleared", "why": "no living hostile is left in the cell",
                    "unvisitedRooms": len(model.unvisited())}
        ranked, unreachable = _ranked(model, hostiles, lambda h: h.get("room"))
        if not ranked:
            return {"status": "stuck", "why": f"{len(hostiles)} hostile(s) left but none could be ranked"}
        nearest = ranked[0]
        return ref_target(nearest, "hostile", f"nearest hostile ({nearest.get('name')}, level {nearest.get('level')})")

    if goal == "loot":
        pending = [c for slot in model.contents.values() for c in slot["containers"]
                   if c["refID"] not in memory.approached and "ref:" + c["refID"] not in skip]
        pending += [i for slot in model.contents.values() for i in slot["items"]
                    if (i.get("value") or 0) >= 100 and i["refID"] not in memory.approached and "ref:" + i["refID"] not in skip]
        ranked, _ = _ranked(model, pending, lambda e: e.get("room"))
        if not ranked:
            return {"status": "looted", "why": "every known container and valuable item has been walked up to"}
        nearest = ranked[0]
        kind = "container" if "entries" in nearest or "size" in nearest else "item"
        return ref_target(nearest, kind, "nearest unvisited " + kind)

    if goal == "exit":
        if not model.exit:
            doors = [d for slot in model.contents.values() for d in slot["doors"] if d.get("teleports")]
            return {"status": "no_exit", "why": "no load door leads to the exterior in this cell",
                    "loadDoors": [{"refID": d["refID"], "name": d.get("name"), "room": d.get("room")} for d in doors]}
        if near(model.exit):
            return {"status": "at_exit", "exit": model.exit["refID"], "why": "already at the exit door"}
        return ref_target(model.exit, "exit door", "the load door to the exterior")

    if goal.startswith("find:"):
        text = goal[5:].strip()
        hits = [e for _, e in model.find(text) if e["refID"] not in memory.approached and "ref:" + e["refID"] not in skip]
        if hits:
            ranked, _ = _ranked(model, hits, lambda e: e.get("room"))
            pick = ranked[0] if ranked else hits[0]
            if near(pick):
                return {"status": "found", "ref": pick["refID"], "name": pick.get("name"), "room": pick.get("room"),
                        "pos": pick.get("pos"), "why": f"{pick.get('name')} is right here"}
            return ref_target(pick, "match", f"matches \"{text}\"")
        target, _ = unvisited_room()
        if target:
            target["why"] = f"nothing named \"{text}\" is in view; looking in the nearest unvisited room"
            return target
        return {"status": "not_found", "why": f"nothing named \"{text}\" is in this cell and every reachable room has been seen"}

    if goal not in ("unvisited", "explore"):
        raise ValueError("goal is unvisited, clear, loot, exit or find:<name>")
    target, unreachable = unvisited_room()
    if target:
        return target
    status = {"status": "fully_explored", "why": "every reachable room has been visited"}
    if unreachable:
        status["unreachableRooms"] = unreachable
    return status


# ---- tools ---------------------------------------------------------------------------------------------------

def register(mcp, native, manager, is_busy):
    memories = defaultdict(Memory)
    geometry = {}

    async def read_model(radius=6000, refresh_mesh=False):
        """The current cell as a CellModel, rebuilding the rooms only when the navmesh changed."""
        contents = await native("Cell.contents", radius=radius)
        cell = contents.get("cell") or {}
        cell_id = cell.get("formId") or cell.get("formID") or cell.get("name")
        cached = geometry.get(cell_id)
        interior = contents.get("interior", True)
        if cached is None or refresh_mesh or not interior:
            navmesh = await native("Cell.navmesh", radius=radius)
            signature = (navmesh.get("triangles"), len(navmesh.get("v", ())))
            if cached is None or cached[0] != signature:
                mesh = rooms_module.Mesh.from_reply(navmesh)
                built = rooms_module.build_rooms(mesh)
                cached = (signature, mesh, built, navmesh.get("cell"), navmesh.get("interior", True))
                geometry[cell_id] = cached
        _, mesh, (rooms, owner, passages), cell_info, cell_interior = cached
        visited = await native("Cell.visited")
        model = rooms_module.CellModel(mesh, rooms, owner, passages, cell_info, cell_interior)
        model.reached = set(memories[cell_id].reached)
        model.mark_visits(visited.get("points", []))
        model.add_contents(contents, contents.get("player"))
        return model, cell_id, visited

    def save_dir():
        return Path(manager.config.get("cellMapsDir") or manager.config.get("recordingsDir") or ".local/cellmaps").expanduser()

    @mcp.tool()
    async def cell_map(format: str = "text", plot: bool = False, radius: float = 6000, refresh: bool = False) -> str:
        """Map of the cell the player is in, for places with no quest marker (Cell.navmesh + Cell.contents +
        Cell.visited, grouped client-side): rooms found by clustering the navmesh and splitting it at chokepoints,
        with per room the centre, size, level, links to other rooms (open, passage or door, with length/width) and
        what is there: living hostiles (count, names, levels, a likely boss), containers (a locked or large chest
        near the boss is flagged bossChest), doors (load doors, the one to the exterior marked EXIT), levers and
        other activators, and valuable items; plus whether each room was visited (player positions sampled while
        in the cell). format "text" is one line per room; "json" is the full structure. plot=true also writes a
        top-down SVG next to the recordings (.local) and returns its path. Works in exteriors within `radius` of
        the player. refresh=true rebuilds the rooms from a fresh navmesh read. Walk to a room with
        nav_goto {"x","y","z"} from its anchor, or use explore."""
        if format not in ("text", "json"):
            raise ValueError("format is text or json")
        model, cell_id, visited = await read_model(radius, refresh)
        out = {}
        if plot:
            folder = save_dir()
            folder.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str((model.cell or {}).get("name") or cell_id))
            svg = folder / f"cellmap-{name}.svg"
            svg.write_text(rooms_module.plot_svg(model, title=str((model.cell or {}).get("name") or cell_id),
                                                 visited_points=visited.get("points", [])), encoding="utf-8")
            out["plot"] = str(svg)
        if format == "text":
            text = rooms_module.describe(model)
            return text + (f"\nplot: {out['plot']}" if plot else "")
        return dumps({**rooms_module.cell_summary(model), **out,
                      "roomList": [rooms_module.room_summary(model, rid) for rid in model.rooms]})

    async def wait_nav(timeout_s):
        began = time.monotonic()
        loading = 0.0
        while True:
            tick = time.monotonic()
            try:
                status = await native("Nav.status")
            except Exception as error:
                if not is_busy(error):
                    raise
                await asyncio.sleep(0.5)
                loading += time.monotonic() - tick
                continue
            if not status.get("active"):
                return status.get("last") or {}
            if status["nav"].get("loading"):
                loading += time.monotonic() - tick + 0.5
            if time.monotonic() - began - loading >= timeout_s:
                await native("Nav.cancel")
                return {**status["nav"], "state": "failed", "reason": "leg timeout"}
            await asyncio.sleep(1.0)

    async def run_fight(style, stand_ground):
        content = await mcp.call_tool("combat_fight", {"style": style, "stand_ground": stand_ground, "timeout_s": 120,
                                                       "rounds": 80})
        content = content[0] if isinstance(content, tuple) else content
        return json.loads(content[0].text)

    async def open_state(ref):
        """open/closed/... of a gate, bridge or door, or None when it cannot be read."""
        if not ref:
            return None
        try:
            info = await native("World.inspect", refID=ref, nodes=False, graph=False)
        except Exception:
            return None
        return info.get("openState")

    async def pull_for(model, memory, near, pulls, log, blockers=None):
        """Walk to the best lever and pull it, then re-read the blocker. True when the blocker changed or when a
        lever whose effect is unknown was pulled (the caller retries the walk)."""
        blocker = blocker_ref(blockers)
        state = await open_state(blocker)
        if state in OPEN_STATES:
            log(f"blocker {blocker} is already {state}: no lever needed")
            return False
        for lever in lever_candidates(model, near, memory)[:3]:
            ref = lever["refID"]
            first = ref not in memory.pulled
            log(f"{'pull' if first else 'pull again'} lever {ref} ({lever.get('name')}) for blocker {blocker or 'unknown'} ({state})")
            await native("Nav.goto", goal={"refID": ref}, activate=True, onCombat="ignore")
            result = await wait_nav(NAV_LEG_TIMEOUT_S)
            entry = {"ref": ref, "name": lever.get("name"), "outcome": result.get("state"), "reason": result.get("reason")}
            pulls.append(entry)
            if result.get("state") != "arrived":
                memory.pulled.add(ref)
                log(f"lever {ref}: could not reach it ({result.get('reason')})")
                continue
            await asyncio.sleep(SETTLE_S)
            after = await open_state(blocker)
            effect = record_pull(memory, ref, blocker, state, after)
            entry.update(blocker=blocker, before=state, after=after, changed=effect["changed"])
            log(f"lever {ref}: blocker {blocker} {state} -> {after}" + (" (changed)" if effect["changed"] else ""))
            if effect["changed"] is False:
                continue
            return True
        return False

    async def unlock_for(model, memory, near, log):
        """Unlock the easy locked doors around an unreachable hostile. True when one was opened."""
        did = False
        for door in door_candidates(model, near, memory)[:2]:
            if len(memory.unlocked) >= UNLOCKS_PER_CELL:
                break
            ref = door["refID"]
            memory.unlocked.add(ref)
            log(f"unlock door {ref} ({door.get('name')}, {door.get('lockLevel')}) near the unreachable hostile")
            try:
                await native("Console.execute", command=f"prid {ref}", target="none")
                lines = await native("Console.execute", command="unlock", target="selected")
                log(f"unlock {ref}: {' | '.join(str(x) for x in (lines or {}).get('lines', []))[:120] or 'done'}")
                did = True
            except Exception as error:
                log(f"unlock {ref} failed: {error}")
        return did

    @mcp.tool()
    async def explore(goal: str = "unvisited", fight: bool = False, invulnerable: bool = False,
                      rooms: int = 1, timeout_s: float = 300, activate: bool = False, style: str = "melee",
                      stand_ground: bool = False, unlock: bool = True) -> str:
        """Find the next thing to walk to in a cell with no quest marker, walk there, and report what was found.
        goal: "unvisited" (default: the nearest unvisited room), "clear" (the nearest living hostile, until none is
        left: "cell cleared"), "loot" (the nearest container or valuable item not yet walked up to), "exit" (the
        load door to the exterior; activate=true also goes through) or "find:<name>" (a reference whose name
        contains the text, else the nearest unvisited room). Each leg is a Nav.goto; `rooms` legs are walked per
        call (default 1; use 99 to run to the end), within timeout_s. fight=true answers
        combat with combat_fight (style, stand_ground as there) and then carries on; invulnerable=true turns god
        mode on for the walk. Without it, combat is handled by Nav.goto's default (run past). The reply has a
        status (budget: more to do; final: cleared, fully_explored, found, looted, at_exit, arrived_at_exit,
        left_cell, hostile_reached, not_found, no_exit, stuck, dead, outmatched), the legs walked with their outcome, hostiles seen to be
        gone, levers pulled to open a shut way, and the map summary. Levers toggle, so each is pulled once and the
        blocker's open state is read afterwards; a lever is pulled again only when it changed nothing. Hostiles that
        stay unreachable (nav fails, "Interrupted by combat" with no way to them) are given up on after two failed
        legs and exploring goes on; when only those are left the status is cleared_reachable and `unreachable` says
        why for each. unlock=true opens doors locked at very easy or easy near such a hostile (console unlock, noted
        in the log). Every decision is written to explore-<cell>.log in the recordings/cell maps folder and the last
        lines are in the reply as `log`."""
        fight_on_contact = fight
        began = time.monotonic()
        legs = []
        killed = []
        failures_in_row = 0
        status = {"status": "budget", "why": "the leg budget for this call is used up"}
        god = False
        log = StepLog()
        log_ready = False
        memory = None
        cell_id = None
        pulls = []
        force_mesh = False
        try:
            while True:
                model, cell_id, _ = await read_model(refresh_mesh=force_mesh)
                force_mesh = False
                memory = memories[cell_id]
                if not log_ready:
                    log_ready = True
                    try:
                        folder = save_dir()
                        folder.mkdir(parents=True, exist_ok=True)
                        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str((model.cell or {}).get("name") or cell_id))
                        log.path = str(folder / f"explore-{name}.log")
                    except OSError:
                        log.path = None
                    log(f"explore goal={goal} fight={fight} rooms={rooms} in {cell_id}; {len(model.hostiles())} hostile(s), "
                        f"{len(model.unvisited())} unvisited room(s)")
                alive_before = {h["refID"]: h.get("name") for h in model.hostiles()}
                if model.player_room:
                    memory.reached.add(model.player_room)
                pick = choose_target(model, goal, memory)
                if pick["status"] != "go":
                    if pick["status"] in ("fully_explored", "stuck", "not_found"):
                        # rooms or hostiles are left that nothing reaches: an easy lock or a lever may open the way
                        if unlock and pick.get("hostiles"):
                            reopened = False
                            for h in model.hostiles():
                                if "ref:" + h["refID"] in memory.blocked and await unlock_for(model, memory, h.get("pos"), log):
                                    reopened = True
                                    memory.blocked.discard("ref:" + h["refID"])
                                    memory.failures.pop("ref:" + h["refID"], None)
                            if reopened:
                                force_mesh = True
                                continue
                        if len(memory.pulled) < PULLS_PER_CELL and await pull_for(model, memory, model.player, pulls, log):
                            force_mesh = True
                            continue
                    status = pick
                    if goal.strip() == "clear" and pick["status"] == "stuck" and pick.get("hostiles"):
                        status = {"status": "cleared_reachable",
                                  "why": f"every reachable hostile is dead; {len(pick['hostiles'])} could not be reached or killed",
                                  "unreachable": [{**h, "why": memory.unreachable.get("ref:" + h["refID"])} for h in pick["hostiles"]]}
                    log(f"final {status['status']}: {status.get('why')}")
                    break
                if len(legs) >= max(1, rooms):
                    status = {"status": "budget", "why": f"{len(legs)} leg(s) walked; call explore again to continue",
                              "next": {k: pick[k] for k in ("kind", "room", "name", "why") if pick.get(k)}}
                    break
                if time.monotonic() - began > timeout_s:
                    status = {"status": "budget", "why": f"timeout_s {timeout_s:g} used up", "next": {k: pick[k] for k in ("kind", "room", "name", "why") if pick.get(k)}}
                    break
                if invulnerable and not god and not model.god_mode:
                    await native("Console.execute", command="tgm", target="none")
                    god = True
                goal_spec = {"refID": pick["ref"]} if pick["kind"] == "ref" else \
                    {"x": pick["pos"][0], "y": pick["pos"][1], "z": pick["pos"][2]}
                leg_began = time.monotonic()
                log(f"leg {len(legs) + 1}: {pick['why']} -> {pick.get('name') or pick['room']} ({pick['kind']})")
                leg = {"to": pick.get("name") or pick["room"], "room": pick.get("room"), "why": pick["why"]}
                nav_args = dict(goal=goal_spec, onCombat="stop" if fight_on_contact else None)
                if pick["kind"] == "ref" and pick.get("kindOf") == "exit door" and activate:
                    nav_args["activate"] = True
                await native("Nav.goto", **nav_args)
                result = await wait_nav(NAV_LEG_TIMEOUT_S)
                fights = []
                at_hostile = pick.get("kindOf") == "hostile"
                while fight_on_contact and len(fights) < FIGHTS_PER_LEG:
                    contact = result.get("state") == "failed" and str(result.get("reason", "")).startswith("Interrupted by combat")
                    if not contact and not (at_hostile and result.get("state") == "arrived"):
                        break
                    outcome = await run_fight(style, stand_ground)
                    fights.append({"outcome": outcome.get("outcome"), "rounds": outcome.get("rounds"),
                                   "lowestHealthPct": outcome.get("lowestHealthPct"), "hostilesLeft": outcome.get("hostilesLeft")})
                    if outcome.get("outcome") in ("dead", "outmatched", "retreated"):
                        status = {"status": "dead" if outcome["outcome"] == "dead" else "outmatched",
                                  "why": outcome.get("assessment") or outcome["outcome"], "advice": outcome.get("advice")}
                        break
                    # Nav.goto treats a hit in the last few seconds as combat, so let that window pass
                    await asyncio.sleep(HIT_WINDOW_S)
                    if at_hostile:
                        break
                    await native("Nav.goto", **nav_args)
                    result = await wait_nav(NAV_LEG_TIMEOUT_S)
                if result.get("leversPulled"):
                    log(f"Nav.goto pulled {result['leversPulled']} lever(s) itself during this leg")
                if fights and at_hostile and result.get("state") == "failed" and fights[-1]["outcome"] == "clear" and not status.get("status") in ("dead", "outmatched"):
                    # the interruption was the fight itself, and it was won: the leg did its job
                    result = {**result, "state": "arrived", "reason": "fought on arrival"}
                leg.update({"outcome": result.get("state"), "reason": result.get("reason"),
                            "elapsedS": round(time.monotonic() - leg_began, 1)})
                if fights:
                    leg["fights"] = fights
                legs.append(leg)
                log(f"leg {len(legs)}: {leg['outcome']} ({leg.get('reason')}) in {leg['elapsedS']}s"
                    + (f"; fights {[f['outcome'] for f in fights]}" if fights else ""))
                arrived = result.get("state") == "arrived"
                if arrived:
                    failures_in_row = 0
                    if pick["kind"] == "room":
                        memory.reached.add(pick["room"])
                    else:
                        memory.approached.add(pick["ref"])
                    if pick.get("kindOf") == "match":
                        status = {"status": "found", "ref": pick["ref"], "name": pick.get("name"), "room": pick.get("room"),
                                  "why": f"walked up to {pick.get('name')}"}
                    elif pick.get("kindOf") == "exit door":
                        status = {"status": "arrived_at_exit" if not activate else "left_cell", "exit": pick["ref"]}
                    elif at_hostile and not fight_on_contact:
                        status = {"status": "hostile_reached", "ref": pick["ref"], "name": pick.get("name"), "room": pick.get("room"),
                                  "advice": "call combat_fight, or explore again with fight=true, to deal with it"}
                    elif pick.get("kindOf") in ("container", "item"):
                        status = {"status": "go", "arrivedAt": pick.get("name")}
                elif unlock and pick.get("pos") and await unlock_for(model, memory, pick["pos"], log):
                    # an easy lock may be what shuts the way: try the same target again without counting a failure
                    leg["unlocked"] = True
                    force_mesh = True
                elif blocked_by_gate(result) and len(memory.pulled) < PULLS_PER_CELL and await pull_for(
                        model, memory, ((result.get("blockers") or {}).get("stop")) or model.player, pulls, log,
                        result.get("blockers")):
                    # the way was shut and something was operated: try the same target again without counting a failure
                    leg["pulledFor"] = True
                    force_mesh = True
                else:
                    failures_in_row += 1
                    keys = [pick["key"]]
                    enemy = enemy_ref(result) if str(result.get("reason") or "").startswith("Interrupted by combat") else None
                    if enemy and "ref:" + enemy not in keys:
                        keys.append("ref:" + enemy)
                    for key in keys:
                        memory.failures[key] += 1
                        if memory.failures[key] >= GIVE_UP_AFTER and key not in memory.blocked:
                            memory.blocked.add(key)
                            memory.unreachable[key] = str(result.get("reason") or "no route")
                            leg["givenUp"] = True
                            log(f"unreachable: {key} after {memory.failures[key]} failed leg(s) ({memory.unreachable[key]}); moving on")
                    if leg.get("givenUp"):
                        failures_in_row = 0
                if status.get("status") in ("dead", "outmatched", "found", "arrived_at_exit", "left_cell", "hostile_reached"):
                    break
                if failures_in_row >= STUCK_AFTER:
                    status = {"status": "stuck", "why": f"{failures_in_row} legs in a row failed; last reason: {result.get('reason')}"}
                    break
                after, _, _ = await read_model()
                still = {h["refID"] for h in after.hostiles()}
                for ref, name in alive_before.items():
                    if ref not in still:
                        killed.append(name)
                        memory.killed.append(name)
                if at_hostile and arrived and pick["ref"] in still:
                    # fought at it and it is still standing: do not loop on it forever
                    memory.failures[pick["key"]] += 1
                    if memory.failures[pick["key"]] >= GIVE_UP_AFTER + 1:
                        memory.blocked.add(pick["key"])
                        memory.unreachable[pick["key"]] = "still standing after fights"
                        leg["givenUp"] = True
                        log(f"unreachable: {pick['key']} still standing after fights; moving on")
                if status.get("status") == "go":
                    status = {"status": "budget", "why": "continuing"}
        finally:
            if god:
                # only switch it off if it is still the mode we turned on
                contents = await native("Cell.contents", radius=500)
                if contents.get("godMode"):
                    await native("Console.execute", command="tgm", target="none")
        summary = {}
        try:
            model, cell_id, _ = await read_model()
            summary = rooms_module.cell_summary(model)
            if status.get("status") == "cleared":
                status["why"] += "; " + (f"{summary['unvisited']} room(s) unvisited" if summary["unvisited"] else "every room was visited")
        except Exception as error:  # the cell may have changed under us
            summary = {"note": f"map unavailable: {error}"}
        out = {**status, "goal": goal, "legs": legs, "logFile": log.path, "log": log.lines[-60:], **({"pulls": pulls} if pulls else {}), "killed": dict(Counter(killed)),
               "elapsedS": round(time.monotonic() - began, 1), "map": summary}
        return dumps(out)

    return mcp

