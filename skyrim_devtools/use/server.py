"""MCP tool surface for playtesting Skyrim through the devtools plugin."""

import argparse
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import time

from mcp.server.fastmcp import FastMCP, Image

from . import capture, combat, perception, profile_tools, saves
from .manager import GameError, GameManager
from .timeline import Recorder

INSTRUCTIONS = """\
Playtest Skyrim SE (offline test profile) through an injected plugin. Call game_start first (headless by
default, about 30-90 s), then observe with snapshot, act with console / press_key / open_menu / look, and
verify with state tools. Time rules: the game ignores all input while the load fade is showing and a menu
ignores input for about 0.3 s after it opens; use wait_for instead of fixed sleeps. Call game_stop when done.
Anything the plugin can do that has no curated tool is reachable via rpc_methods + rpc_call."""

# Curated wrappers for native methods that older plugin builds lack.
UNAVAILABLE = "method {} is not available in this plugin build (see rpc_methods); update the plugin or use another tool"


FPS_WARN = 30.0
P99_WARN_MS = 100.0


def perf_verdict(stats, hitches, seconds):
    """Condense Perf.frameStats and Perf.hitches into one reading with warnings."""
    result = {"seconds": seconds, "frames": stats.get("count"), "fps": stats.get("fps"), "p50Ms": stats.get("p50Ms"),
              "p99Ms": stats.get("p99Ms"), "maxMs": stats.get("maxMs"), "hitchCount": len(hitches.get("hitches", [])),
              "worstHitches": sorted(hitches.get("hitches", []), key=lambda h: -h["ms"])[:3],
              "slowCalls": hitches.get("slowCalls", {}), "warnings": []}
    fps, p99 = stats.get("fps"), stats.get("p99Ms")
    if fps is None:
        result["warnings"].append("no frames were presented in the window: the game is stalled or minimised")
    else:
        if fps < FPS_WARN:
            result["warnings"].append(f"fps {fps} is under {FPS_WARN:g}: play is unreliable; see docs/skyrim-use.md, Performance")
        if p99 is not None and p99 > P99_WARN_MS:
            result["warnings"].append(f"p99 frame {p99} ms is over {P99_WARN_MS:g} ms: expect stutter")
    result["ok"] = not result["warnings"]
    return result


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def summarize(state):
    menus = state.get("menus", [])
    return {
        "playable": state.get("playable"),
        "openMenus": [m["name"] for m in menus if m.get("ready", True)],
        "menusStillOpening": [m["name"] for m in menus if not m.get("ready", True)],
        "foreground": state.get("foreground"),
        "loadEpoch": state.get("loadEpoch"),
        "loadCompleted": state.get("loadCompleted"),
        "loadSucceeded": state.get("loadSucceeded"),
        "lastSave": state.get("lastSave"),
        "seq": state.get("seq"),
    }


def dig(value, path):
    """Resolve a dotted path with numeric list indices; missing steps give None."""
    for part in path.split("."):
        if isinstance(value, list) and part.lstrip("-").isdigit():
            try:
                value = value[int(part)]
            except IndexError:
                return None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def image_parts(result):
    """Accept the plausible shapes of an image payload and return (bytes, format) or None."""
    if not isinstance(result, dict):
        return None
    data = next((result[key] for key in ("data", "image", "base64", "png", "jpeg") if isinstance(result.get(key), str)), None)
    if data is None:
        return None
    fmt = str(result.get("format") or result.get("mime") or result.get("mimeType") or "png").lower()
    fmt = "jpeg" if "jp" in fmt else "png"
    try:
        return base64.b64decode(data), fmt
    except ValueError:
        return None


_STARTED = time.time()


def source_mtime():
    """Newest change among the client's own Python files."""
    root = Path(__file__).resolve().parent.parent
    return max((path.stat().st_mtime for path in root.rglob("*.py")), default=0.0)


def daemon_state():
    """Whether the running process predates its sources, so a rebuilt or pulled tree is not silently ignored."""
    changed = source_mtime()
    state = {"stale": changed > _STARTED + 1}
    if state["stale"]:
        state["warning"] = "the skyrim-use server started before its code last changed; restart it to use the new tools"
    return state


def is_busy_error(error):
    """The game did not answer in time or the link is being recovered: what a cell load looks like from here."""
    text = str(error)
    return "did not answer in time" in text or "-32008" in text


def build_server(manager, recorder=None):
    recorder = recorder or Recorder()

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        recorder.start_sampler(manager)
        try:
            yield
        finally:
            await recorder.stop_sampler()
            # a game left running would hold the single test PC after the client is gone
            with contextlib.suppress(Exception):
                await manager.stop()

    mcp = FastMCP("skyrim-use", instructions=INSTRUCTIONS, lifespan=lifespan)
    recorder.instrument(mcp)

    async def native(method, **params):
        """Call a method that may be missing from the running plugin build."""
        params = {key: value for key, value in params.items() if value is not None}
        if manager.alive and manager.methods and not manager.has_method(method):
            raise GameError(UNAVAILABLE.format(method))
        return await manager.call(method, **params)

    # ---- lifecycle ----------------------------------------------------------------------------

    @mcp.tool()
    async def game_start(headless: bool = True, save: str | None = None) -> str:
        """Launch Skyrim on the test PC and connect (30-90 s). headless=true renders no window.
        Optionally load `save` (a name from list_saves, no extension) and wait until the world is playable.
        Idempotent: if a game is already running its status is returned. A crashed game is reported
        by game_status/any tool; call game_start again to relaunch. With a window (headless=false), call
        perf_check next: a slideshow game wastes the whole session."""
        return dumps(await manager.start(headless=headless, save=save))

    @mcp.tool()
    async def game_stop(grace_seconds: int = 12) -> str:
        """Close the game and release the connection. Always call this when finished. The game is asked to quit
        and killed if it has not exited after grace_seconds (1-120); `path` in the result says which way it
        ended (graceful, killed-after-Ns, closing-crashed) and `crash` carries the crash log's top frames."""
        return dumps(await manager.stop(grace=max(1, min(120, grace_seconds))))

    @mcp.tool()
    async def game_status() -> str:
        """Phase (idle/starting/running/crashed/stopped/failed), session info and the crash reason if the game
        died (`crash.log` holds the crash logger's exception and top frames when one was written). Probes the live
        link, so it notices a crash that happened while you were idle. `daemon.stale` is true when this MCP
        process started before its own source files were last changed: restart it to pick the changes up."""
        status = await manager.status()
        status["daemon"] = daemon_state()
        return dumps(status)

    # ---- observation --------------------------------------------------------------------------

    @mcp.tool()
    async def snapshot() -> str:
        """Compact UI/world readiness: playable (a save is loaded and no blocking load), open menus, loadEpoch
        (increments on every save load), lastSave. Cheap; call it before and after actions."""
        return dumps(summarize(await manager.call("Events.snapshot")))

    @mcp.tool()
    async def list_menus(open_only: bool = True) -> str:
        """Registered game menus; by default only those open now. Names are e.g. InventoryMenu, MapMenu,
        Journal Menu, Console, Main Menu, Fader Menu (a fade is on screen: input is ignored)."""
        menus = await manager.call("UI.listMenus")
        return dumps([m["name"] for m in menus if m["open"]] if open_only else menus)

    @mcp.tool()
    async def open_menu(name: str) -> str:
        """Open a menu by exact name (see list_menus(open_only=false)) and wait until it is open. Requires a
        playable world. A fresh menu ignores key input for about 0.3 s; wait_for or pause before pressing keys."""
        return dumps(await manager.call("UI.openMenu", name=name, wait=True))

    @mcp.tool()
    async def close_menu(name: str) -> str:
        """Close a menu by exact name and wait until it is closed."""
        return dumps(await manager.call("UI.closeMenu", name=name, wait=True))

    @mcp.tool()
    async def console(command: str, target: str = "selected") -> str:
        """Run a Skyrim console command and return the lines it printed. target: `selected` (the reference
        picked in the console, default), `player` (use for GetAV/AddItem/player.* style commands) or `none`.
        Needs a loaded world."""
        result = await manager.call("Console.execute", command=command, target=target)
        return "\n".join(result["lines"]) or "(no output)"

    # ---- input --------------------------------------------------------------------------------

    @mcp.tool()
    async def press_key(key: str, phase: str = "tap", hold_ms: int = 60) -> str:
        """Inject a keyboard key (name like Tab, E, W, Escape, Grave, F1, or DirectInput scancode 1-255).
        phase `tap` presses and releases after hold_ms (0-10000 ms) and returns once released; use a long
        hold_ms to walk (W for 1500) or hold. `down`/`up` hold across calls; press_release_all frees stuck keys.
        Ignored during load fades; menus ignore it for ~0.3 s after opening. Menu lists (inventory, magic,
        journal) do not take key presses for navigation: use press_control (Accept, Cancel, Up, Down, Left,
        Right) or ui_select_item instead."""
        return dumps(await manager.call("Input.key", key=key, phase=phase, holdMs=hold_ms))

    @mcp.tool()
    async def press_control(name: str, phase: str = "tap", hold_ms: int = 60) -> str:
        """Fire a game control by user-event name (Activate, Journal, Tween Menu, Jump, Sneak, Forward,
        Ready Weapon ...), resolved to whatever key is bound in the current input context. Preferred over
        press_key because it works inside menus too: Accept, Cancel, Up, Down, Left, Right move through the
        inventory, magic and journal lists. hold_ms 0-10000."""
        return dumps(await manager.call("Input.control", name=name, phase=phase, holdMs=hold_ms))

    @mcp.tool()
    async def mouse(dx: int = 0, dy: int = 0, button: str | None = None, phase: str = "tap", hold_ms: int = 60) -> str:
        """Relative mouse motion in pixels (dx right, dy down) or a button click (`left`, `right`, `middle`).
        Give either motion or a button. For turning the camera, prefer look."""
        params = {"button": button, "phase": phase, "holdMs": hold_ms} if button else {"dx": dx, "dy": dy}
        return dumps(await manager.call("Input.mouse", **params))

    @mcp.tool()
    async def release_all_input() -> str:
        """Release every held key/button (recover from a stuck `down`)."""
        return dumps(await manager.call("Input.releaseAll"))

    @mcp.tool()
    async def look(yaw_deg: float | None = None, pitch_deg: float | None = None) -> str:
        """Turn the player relative to the current view, in degrees: yaw positive turns right, pitch positive
        looks down (clamped to +-85). Needs a loaded world."""
        params = {k: v for k, v in (("yawDeg", yaw_deg), ("pitchDeg", pitch_deg)) if v is not None}
        return dumps(await manager.call("Player.look", **params))

    # ---- saves and world ----------------------------------------------------------------------

    @mcp.tool()
    async def list_saves() -> str:
        """Save names available to load_save (isolated test profile), with sizes in bytes."""
        return dumps(await manager.call("Session.listSaves"))

    @mcp.tool()
    async def load_save(name: str, wait: bool = True) -> str:
        """Load a save by name (works from the main menu or in game). Increments loadEpoch. With wait=true
        returns when the load finishes, then call wait_for(playable=true): the fade-in still ignores input."""
        return dumps(await manager.call("Session.loadSave", timeout=120, name=name, wait=wait))

    @mcp.tool()
    async def reload(wait: bool = True) -> str:
        """Reload the last loaded save (fast reset between test attempts). Same timing rules as load_save."""
        return dumps(await manager.call("Session.reload", timeout=120, wait=wait))

    @mcp.tool()
    async def center_on_cell(cell: str) -> str:
        """Teleport the player to a cell by editor id (console `coc`), e.g. WhiterunBanneredMare. Returns console lines."""
        return dumps(await manager.call("World.centerOnCell", cell=cell))

    # ---- waiting and events -------------------------------------------------------------------

    @mcp.tool()
    async def wait_for(
        timeout_s: float = 30,
        menu_open: str | None = None,
        menu_closed: str | None = None,
        playable: bool | None = None,
        event_type: str | None = None,
        fresh_events_only: bool = False,
        method: str | None = None,
        path: str | None = None,
        equals: str | None = None,
        poll_ms: int = 250,
    ) -> str:
        """Block until ALL given conditions hold or timeout_s passes (max 300). Conditions: menu_open /
        menu_closed (menu name), playable (true = a save is loaded AND the load fade is gone, i.e. input works), event_type (a buffered event of that type exists; counts
        events not yet drained unless fresh_events_only=true, and does not consume them), and a generic RPC
        probe: method (e.g. Player.state) with path (dotted, list indexes numeric, e.g. location.cell) whose
        value, compared as JSON text or plain string, must equal `equals`. Returns {ok, waitedMs, state}.
        Prefer this over fixed sleeps."""
        if not any(x is not None for x in (menu_open, menu_closed, playable, event_type, method)):
            raise GameError("give at least one condition")
        if method and (path is None or equals is None):
            raise GameError("method needs path and equals")
        timeout_s = min(max(timeout_s, 0.1), 300)
        start_total = manager.event_total
        began = time.monotonic()
        last = {}
        while True:
            last = {}
            ok = True
            if any(x is not None for x in (menu_open, menu_closed, playable)):
                state = summarize(await manager.call("Events.snapshot"))
                last["snapshot"] = state
                if menu_open is not None and menu_open not in state["openMenus"]:
                    ok = False
                if menu_closed is not None and (menu_closed in state["openMenus"] or menu_closed in state["menusStillOpening"]):
                    ok = False
                if playable is not None:
                    # playable turns true while the load fade is still on screen and input is still ignored
                    settled = bool(state["playable"]) and "Fader Menu" not in state["openMenus"]
                    if settled != playable:
                        ok = False
            if event_type is not None:
                events, _ = manager.events_since(start_total if fresh_events_only else manager.drain_cursor)
                hits = [e for e in events if e["type"] == event_type]
                last["event"] = hits[-1] if hits else None
                if not hits:
                    ok = False
            if method:
                value = dig(await native(method), path)
                last["value"] = value
                if not (value == equals or dumps(value) == equals or str(value) == equals):
                    ok = False
            if ok:
                return dumps({"ok": True, "waitedMs": int((time.monotonic() - began) * 1000), "state": last})
            if time.monotonic() - began >= timeout_s:
                return dumps({"ok": False, "waitedMs": int((time.monotonic() - began) * 1000), "state": last})
            await asyncio.sleep(max(poll_ms, 50) / 1000)

    @mcp.tool()
    async def events_subscribe(types: list[str]) -> str:
        """Choose which native events are buffered (use ["*"] for all, [] none). Default after game_start: every
        declared event except console. Types: menuOpenClose, loadGame, cellChange, plus whatever the plugin
        declares (see events_list). Replaces the current selection."""
        manager.subscribed = list(types)
        await manager.call("Events.unsubscribe", types=["*"])
        if types:
            await manager.call("Events.subscribe", types=list(types))
        return dumps({"subscribed": manager.subscribed})

    @mcp.tool()
    async def events_list() -> str:
        """Declared event types with descriptions and whether each is subscribed."""
        return dumps(await manager.call("Events.list"))

    @mcp.tool()
    async def events_drain(types: list[str] | None = None, limit: int = 200) -> str:
        """Return buffered events since the previous drain, oldest first, and mark them consumed. Each is
        {type, seq, epoch, data}; epoch is the loadEpoch when it happened. `lostEvents` > 0 means the buffer
        overflowed. Optionally filter by types (non-matching events are discarded)."""
        taken, lost, remaining = manager.drain_events(set(types) if types else None, max(1, min(limit, 1000)))
        return dumps({"events": taken, "lostEvents": lost, "remaining": remaining})

    # ---- generic ------------------------------------------------------------------------------

    @mcp.tool()
    async def rpc_methods() -> str:
        """Every native method of the connected plugin with its thread and timeout. Use with rpc_call to reach
        features that have no curated tool."""
        if not manager.alive:
            raise GameError(f"no game is running (phase: {manager.phase}); call game_start first")
        await manager.status()
        return dumps(sorted(manager.methods.values(), key=lambda m: m["name"]))

    @mcp.tool()
    async def rpc_call(method: str, params: dict | None = None, timeout_s: float | None = None) -> str:
        """Call any native JSON-RPC method by name with a params object (see rpc_methods and docs/protocol.md).
        Returns the JSON result. Errors carry the RPC code (-32004 not ready, -32005 stale epoch, -32008 timeout)."""
        return dumps(await manager.call(method, timeout=timeout_s, **(params or {})))

    # ---- curated wrappers for newer native methods --------------------------------------------

    @mcp.tool()
    async def player_state(avs: list[str] | None = None) -> str:
        """Player position, rotation, cell, actor values and flags (Player.state). avs lists extra actor
        values to include, e.g. ["Health", "Magicka"]. Needs a loaded world."""
        return dumps(await native("Player.state", avs=avs))

    @mcp.tool()
    async def player_inventory(filter: str | None = None, limit: int | None = None) -> str:
        """Player inventory with counts and equipped flags (Player.inventory). filter is a case-insensitive
        name substring; limit 1-500 (default 100)."""
        return dumps(await native("Player.inventory", filter=filter, limit=limit))

    @mcp.tool()
    async def world_nearby(radius: float | None = None, types: list[str] | None = None, limit: int | None = None) -> str:
        """References around the player (World.nearby). radius in game units (default 1000, max 20000; ~70
        units is 1 m); types filters by reference kind (lower case: actor, container, door, item, activator, furniture, flora);
        doors carry locked, lockLevel and destination {cell, location, interior}; limit 1-200 (default 20)."""
        return dumps(await native("World.nearby", radius=radius, types=types, limit=limit))

    @mcp.tool()
    async def world_crosshair() -> str:
        """What the crosshair points at (World.crosshair): the reference and its base form, if any."""
        return dumps(await native("World.crosshair"))

    @mcp.tool()
    async def quest_get(editor_id: str | None = None, form_id: str | None = None, all_objectives: bool = False) -> str:
        """One quest's stage, status and objectives (Quest.get); give editor_id (e.g. MQ101) or form_id (hex).
        `status` is running, completed, failed, stopped (stopped without finishing) or not-started;
        `finished` is true for completed or failed. The raw `completed` flag only
        reflects a script's CompleteQuest and `running` stays true after a quest stops, so assert on `status` or
        `finished` instead. all_objectives includes finished and hidden ones."""
        return dumps(await native("Quest.get", editorID=editor_id, formID=form_id,
                                  allObjectives=True if all_objectives else None))

    @mcp.tool()
    async def quest_find(text: str, limit: int | None = None) -> str:
        """Find quests whose display name or editor ID contains `text` (Quest.find), with their editor IDs,
        running flag and stage. Use it to get the editor_id for quest_get."""
        return dumps(await native("Quest.find", text=text, limit=limit))

    @mcp.tool()
    async def activate(ref_id: str, max_distance: float | None = None, force: bool = False) -> str:
        """Activate a reference (door, container, NPC, item, switch) as the player's activate key would,
        without aiming the crosshair (Ref.activate). ref_id is hex from world_nearby. Checks that the player
        is within reach (default 300 units, measured to the nearer of the origin and the bounds centre) and
        answers activated:false, reason "out of reach" otherwise; walls in between are NOT checked
        (lineOfSightChecked:false). force=true skips the reach check. A door starts a load: wait_for
        playable afterwards."""
        return dumps(await native("Ref.activate", refID=ref_id, maxDistance=max_distance, force=True if force else None))

    @mcp.tool()
    async def quest_targets(quest: str | None = None, limit: int | None = None, include_completed: bool = False) -> str:
        """Where the quest markers point (Quest.targets): for each displayed objective of the quest (editor id, form
        id or name; omitted: every running quest, the tracked one first) the target reference the compass is on
        and, when it lies in another cell, the `tracking` reference to reach first (usually a load door), each
        with refID, name, position, cell and distance from the player (null when in another cell). Use
        nav_goto {"questTarget": quest} to walk there. An empty list means the quest shows no marker now."""
        return dumps(await native("Quest.targets", quest=quest, limit=limit,
                                  includeCompleted=True if include_completed else None))

    @mcp.tool()
    async def quest_active(limit: int | None = None) -> str:
        """Currently active quests with their current stage (Quest.active); limit 1-200 (default 30)."""
        return dumps(await native("Quest.active", limit=limit))

    @mcp.tool()
    async def message_box() -> str:
        """The message box currently on screen: text and its buttons (UI.messageBox)."""
        return dumps(await native("UI.messageBox"))

    @mcp.tool()
    async def message_box_select(index: int | None = None, text: str | None = None) -> str:
        """Press a message box button by 0-based index or by button text (UI.messageBoxSelect)."""
        return dumps(await native("UI.messageBoxSelect", index=index, text=text))

    @mcp.tool()
    async def dialogue_state() -> str:
        """Current conversation (Dialogue.state): phase (closed, npc-speaking, speaking, advancing, topics,
        submenu), selectable, speaker, topics with 0-based index, the last spoken line and a cursor. Works for
        conversations the NPC started too."""
        return dumps(await native("Dialogue.state"))

    @mcp.tool()
    async def dialogue_select(index: int | None = None, text: str | None = None) -> str:
        """Choose a dialogue topic by 0-based index from dialogue_state, or by its text (Dialogue.select).
        Refused while a line is still being spoken; call dialogue_wait first. Then call dialogue_wait to
        collect the NPC's reply and the next topics."""
        return dumps(await native("Dialogue.select", index=index, text=text))

    @mcp.tool()
    async def dialogue_wait(timeout_ms: int | None = None, skip: bool | None = None, since: int | None = None) -> str:
        """Wait until the conversation is ready again (Dialogue.waitIdle). Returns status topics (pick one
        with dialogue_select), idle (open, no topics), submenu (a training/barter/gift menu opened over the
        dialogue: use that menu, close it, then wait again), closed, or timeout, together with the lines
        spoken meanwhile [{speaker, role, text}] and the current state. skip=true skips each line as soon as
        it is captured (fast, and what to use when audio is muted). timeout_ms 100-120000 (default 30000)."""
        return dumps(await native("Dialogue.waitIdle", timeoutMs=timeout_ms, skip=skip, since=since))

    @mcp.tool()
    async def dialogue_advance() -> str:
        """Skip the line being spoken, like a click during speech (Dialogue.advance). The next line or the
        topic list follows; use dialogue_wait to collect it."""
        return dumps(await native("Dialogue.advance"))

    @mcp.tool()
    async def dialogue_close() -> str:
        """Leave the conversation like the player's exit button (Dialogue.close); closes a menu opened from it
        first. Idempotent: already closed answers status closed."""
        result = await native("Dialogue.close")
        if isinstance(result, dict):
            result.pop("state", None)
        return dumps(result)

    @mcp.tool()
    async def ui_list_items(menu: str) -> str:
        """Items shown in an open list menu such as InventoryMenu, MagicMenu, ContainerMenu, BarterMenu,
        GiftMenu (UI.listItems), each with its index. Open the menu first and let it settle."""
        return dumps(await native("UI.listItems", menu=menu))

    @mcp.tool()
    async def ui_select_item(menu: str, index: int | None = None, text: str | None = None,
                             action: str | None = None, count: int | None = None) -> str:
        """Pick an item of an open list menu by index or by name text (UI.selectItem). action is the
        menu-specific verb the plugin reports (e.g. equip, use, take, buy); count for stackable transfers."""
        return dumps(await native("UI.selectItem", menu=menu, index=index, text=text, action=action, count=count))

    @mcp.tool()
    async def ui_dump(menu: str, path: str | None = None, depth: int | None = None) -> str:
        """Bounded Scaleform tree of an open menu (UI.dump), from _root or a dotted path such as
        Menu_mc.ItemCard_mc. depth defaults to 2. Use ui_get for one value."""
        return dumps(await native("UI.dump", menu=menu, path=path, depth=depth))

    @mcp.tool()
    async def ui_get(menu: str, path: str, depth: int | None = None) -> str:
        """Read one ActionScript variable of an open menu (UI.getVariable)."""
        return dumps(await native("UI.getVariable", menu=menu, path=path, depth=depth))

    @mcp.tool()
    async def ui_set(menu: str, path: str, value: object) -> str:
        """Write an ActionScript variable of an open menu (UI.setVariable)."""
        return dumps(await native("UI.setVariable", menu=menu, path=path, value=value))

    @mcp.tool()
    async def ui_invoke(menu: str, path: str, args: list | None = None) -> str:
        """Call an ActionScript function of an open menu (UI.invoke) and return its result."""
        return dumps(await native("UI.invoke", menu=menu, path=path, args=args or []))

    @mcp.tool()
    async def ui_list(menu: str) -> str:
        """Entries of a list menu without an item API (UI.list), for example the main menu or Journal."""
        return dumps(await native("UI.list", menu=menu))

    @mcp.tool()
    async def dialogue_start(ref_id: str | None = None) -> str:
        """Start a conversation by activating an NPC (Dialogue.start); ref_id is hex, default the nearest living
        actor. Returns once the dialogue menu is open (opened true); then call dialogue_wait for the greeting."""
        return dumps(await native("Dialogue.start", refID=ref_id))

    @mcp.tool()
    async def screenshot(max_width: int | None = None, format: str | None = None, quality: int | None = None,
                         include_ui: bool | None = None) -> list:
        """Capture the game's own rendered frame as an image (Screen.capture); works headless. max_width
        (default 1024, never upscales), format png or jpeg, quality 1-100 for jpeg, include_ui false hides
        menus and the HUD for the shot."""
        result = await native("Screen.capture", maxWidth=max_width, format=format, quality=quality, includeUI=include_ui)
        parts = image_parts(result)
        if parts is None:
            return [dumps(result)]
        meta = {k: v for k, v in result.items() if k not in {"data", "image", "base64", "png", "jpeg"}}
        return [Image(data=parts[0], format=parts[1]), dumps(meta)]

    @mcp.tool()
    async def perf_frame_stats(window: float | None = None) -> str:
        """Frame timing (Perf.frameStats): fps, frame-time percentiles in ms and recent hitches. window is
        the look-back in seconds (default: everything buffered)."""
        return dumps(await native("Perf.frameStats", window=window))

    @mcp.tool()
    async def perf_hitches(limit: int | None = None, since_frame: int | None = None, clear: bool | None = None) -> str:
        """Frames over the hitch-log threshold (Perf.hitches, default 100 ms) with what the frame thread was doing
        (threadCpuMs vs blockedMs), process page faults and disk traffic during the hitch, and the plugin's own
        main-thread calls that overlapped it. slowCalls tallies every handler that held the game thread over 5 ms."""
        return dumps(await native("Perf.hitches", limit=limit, sinceFrame=since_frame, clear=clear))

    @mcp.tool()
    async def perf_check(seconds: float = 10.0) -> str:
        """Quick frame-rate reading for the start of a session: waits `seconds` (default 10, 2-60), then reports
        fps, median and p99 frame time and the hitches seen, with a warning when fps is under 30 or p99 is over
        100 ms. Call it first in every playtest, once the world is loaded and before any other tool, and act on
        a warning (see docs/skyrim-use.md, Performance) instead of playing through a slideshow."""
        seconds = min(max(float(seconds), 2.0), 60.0)
        frame = (await native("Perf.frameStats")).get("frame", 0)
        await asyncio.sleep(seconds)
        stats = await native("Perf.frameStats", window=seconds)
        hitches = await native("Perf.hitches", sinceFrame=frame, limit=10)
        return dumps(perf_verdict(stats, hitches, seconds))

    @mcp.tool()
    async def papyrus_log(since: int | None = None, level: str | None = None, limit: int | None = None,
                          contains: str | None = None) -> str:
        """Papyrus log lines (Papyrus.log): script errors, warnings and Debug.Trace output. Pass the last
        returned sequence as `since` to read only new lines; level is the minimum severity (default info);
        contains filters by substring; limit default 500."""
        return dumps(await native("Papyrus.log", since=since, level=level, limit=limit, contains=contains))

    capture.register(mcp, manager, recorder)
    combat.register(mcp, native, manager)
    perception.register(mcp, native, manager)
    saves.register(mcp, manager, native)
    profile_tools.register(mcp, native, manager, recorder)

    # ---- movement, camera, navigation, recording ----------------------------------------------

    @mcp.tool()
    async def walk(direction: str = "forward", duration_ms: int | None = None, distance: float | None = None,
                   run: bool | None = None, sprint: bool | None = None) -> str:
        """Move the player by holding a movement key (Move.walk): direction forward/back/left/right, for
        duration_ms (max 60000) OR until `distance` game units are covered (max 20000; 1 unit ~ 1.4 cm,
        a walking stride ~ 70). run/sprint override the run toggle for this walk. Blocks until done and
        returns the distance actually moved and the displacement; walls stop the player, so compare."""
        limit = (duration_ms / 1000 + 15) if duration_ms else 135
        return dumps(await manager.call("Move.walk", timeout=limit, **{k: v for k, v in dict(
            direction=direction, durationMs=duration_ms, distance=distance, run=run, sprint=sprint).items()
            if v is not None}))

    @mcp.tool()
    async def turn_to(heading_deg: float | None = None, x: float | None = None, y: float | None = None,
                      ref_id: str | None = None, smooth: bool | None = None) -> str:
        """Turn the player to an absolute heading (0 = north/+Y, 90 = east/+X), towards a point x,y, or
        towards a reference (refID hex). Smooth by default (180 deg/s); smooth=false snaps. For relative
        turns use look."""
        return dumps(await native("Move.turnTo", headingDeg=heading_deg, x=x, y=y, refID=ref_id, smooth=smooth))

    @mcp.tool()
    async def jump() -> str:
        """Make the player jump once (Move.jump)."""
        return dumps(await native("Move.jump"))

    @mcp.tool()
    async def sneak(on: bool) -> str:
        """Enter (on=true) or leave sneak mode; returns whether the player is sneaking afterwards."""
        return dumps(await native("Move.sneak", on=on))

    @mcp.tool()
    async def camera_state() -> str:
        """Camera mode (firstPerson/thirdPerson/free/...), camera position, yaw/pitch in degrees, zoom
        (third person, 0-1), field of view, and the player's heading/pitch."""
        return dumps(await native("Camera.state"))

    @mcp.tool()
    async def camera_set(mode: str | None = None, yaw_deg: float | None = None, pitch_deg: float | None = None,
                         zoom: float | None = None, fov: float | None = None) -> str:
        """Switch the camera to mode first/third, set the view yaw/pitch in degrees (turns the player;
        pitch + is down), third-person zoom 0 (close) to 1 (far), or world FOV 20-150."""
        return dumps(await native("Camera.set", mode=mode, yawDeg=yaw_deg, pitchDeg=pitch_deg, zoom=zoom, fov=fov))

    @mcp.tool()
    async def camera_look_at(ref_id: str | None = None, x: float | None = None, y: float | None = None,
                             z: float | None = None) -> str:
        """Aim the player's view at a reference (refID hex, e.g. from a state tool) or a world point x,y,z."""
        return dumps(await native("Camera.lookAt", refID=ref_id, x=x, y=y, z=z))

    @mcp.tool()
    async def nav_nearby(radius: float = 3000, kind: str = "any", limit: int = 30) -> str:
        """Named references around the player, nearest first: refID, name, position, distance and kind
        (actor, door, object; loadDoor marks doors to another cell). radius 100-20000 game units. Use
        the refIDs as goals for nav_goto, turn_to or camera_look_at."""
        return dumps(await native("Nav.nearby", radius=radius, kind=kind, limit=limit))

    @mcp.tool()
    async def nav_places(contains: str = "", limit: int = 50) -> str:
        """Named locations that have a map marker (cities, villages, dungeons): name as shown in the
        game's language, location form id, marker position and worldspace. Use the name or the location
        id as {"place": ...} in nav_goto; on a localized game the names are translated."""
        return dumps(await native("Nav.places", contains=contains or None, limit=limit))

    @mcp.tool()
    async def nav_goto(goal: dict, mode: str = "input", timeout_s: float | None = None, run: bool | None = None,
                       sprint: bool | None = None, on_combat: str | None = None, on_dialogue: str | None = None,
                       on_message_box: str | None = None, on_menu: str | None = None,
                       invulnerable: bool | None = None, via: list | None = None, route: str | None = None,
                       activate: bool | None = None) -> str:
        """Start walking the player to a goal in the background (Nav.goto); returns at once. goal is one of
        {"refID": "0001A2B3"}, {"x":..,"y":..,"z":..}, {"cell": "<editor id>"}, {"place": "Riverwood"} (a
        location name or id from nav_places), {"questTarget": "<quest editor id, id or name>"} (where the compass
        marker points, see quest_targets; "" takes any running quest) or {"near": "<refID>", "radius": 300}. activate: true
        stops within activation reach of a reference goal (90 units unless the goal gives a radius) and activates it
        (open the door, talk to the NPC). Time spent in loading screens does not count against timeout_s. After a
        door into an exterior the trip prefers the long-range route, and the door just used is not taken back.
        mode "input" steers with real key presses
        over the loaded navmesh (replans as cells stream in, jumps/strafes/backs up when stuck, uses a load
        door one hop away); mode "ai" hands the player to the engine's pathfinder. Long trips: input mode
        takes a long-range route over the worldspace graph (built offline by skyrim-devtools-navgraph on the
        test PC) for goals over 8000 units away in the player's worldspace and walks it gate by gate;
        route "global" insists on it, route "local" turns it off.
        Interruptions are handled per policy and reported as navInterrupted/navResumed events: on_combat flee
        (default: keep to the route at a sprint), fight (draw, attack the nearest enemy, then resume), stop or
        ignore; on_dialogue close (default; Cancel, then hide, then the last topic for forced greetings), stop or
        wait; on_message_box firstButton (default), stop or wait; on_menu close (default), stop or wait. Loading,
        knockdown and paralysis are waited out; death fails the trip. A trip resumes from wherever the player is
        once the interruption clears; 20 s without progress triggers recovery and, after 4 rounds, failure.
        invulnerable: true turns on god mode for the trip and restores it after. via: [[x,y,z], ...] waypoints; route: "global",
        "local" or a recording name to follow (input mode). Follow with nav_status / nav_wait; stop with nav_cancel. Speed long
        trips with the console: `sgtm 4` (also speeds everything else; set back with `sgtm 1`)."""
        return dumps(await native("Nav.goto", goal=goal, mode=mode, timeoutS=timeout_s, run=run, sprint=sprint,
                                  onCombat=on_combat, onDialogue=on_dialogue, onMessageBox=on_message_box,
                                  onMenu=on_menu, invulnerable=invulnerable, via=via, route=route,
                                  activate=True if activate else None))

    @mcp.tool()
    async def nav_plan(ref_id: str | None = None, x: float | None = None, y: float | None = None,
                       z: float | None = None, route: str = "local") -> str:
        """Plan without moving: the path over the loaded navmeshes from the player to a reference or point.
        route "global" adds the long-range route over the worldspace graph (global.points, length, cost),
        and the local plan then runs to its first gate. Save the reply to a file and pass it to
        trajectory_plot as route to draw it under a recording."""
        return dumps(await native("Nav.plan", refID=ref_id, x=x, y=y, z=z, route=route))

    @mcp.tool()
    async def nav_status() -> str:
        """The running navigation (state planning/moving/recovering/paused/door, distanceRemaining, etaS,
        stuckCount, replans, waypoint) or, when idle, the last one's outcome and reason."""
        return dumps(await native("Nav.status"))

    @mcp.tool()
    async def nav_wait(timeout_s: float = 120, poll_s: float = 2) -> str:
        """Block until the running navigation ends (arrived/failed/cancelled) or timeout_s (max 300)
        passes; returns the final summary or the current status. Call again to keep waiting. A cell load
        (the game cannot answer for a while) counts as progress: that time is not counted against timeout_s."""
        began = time.monotonic()
        loading_s = 0.0
        timeout_s = min(max(timeout_s, 1), 300)
        while True:
            tick = time.monotonic()
            try:
                status = await native("Nav.status")
            except GameError as error:
                if not is_busy_error(error):
                    raise
                await asyncio.sleep(max(poll_s, 0.25))
                loading_s += time.monotonic() - tick
                continue
            if not status.get("active"):
                return dumps({"done": True, **(status.get("last") or {})})
            if status["nav"].get("loading"):
                loading_s += time.monotonic() - tick + max(poll_s, 0.25)
            if time.monotonic() - began - loading_s >= timeout_s:
                return dumps({"done": False, **status["nav"]})
            await asyncio.sleep(max(poll_s, 0.25))

    @mcp.tool()
    async def nav_cancel() -> str:
        """Stop the running navigation and release the movement keys."""
        return dumps(await native("Nav.cancel"))

    @mcp.tool()
    async def record_start(hz: float | None = None, fields: list[str] | None = None, name: str | None = None) -> str:
        """Record the player's trajectory to a JSONL file on the test PC at hz samples/s (1-60, default 20).
        fields from player, camera, state, input, perf (default all). Stop with record_stop."""
        return dumps(await native("Record.start", hz=hz, fields=fields, name=name))

    @mcp.tool()
    async def record_stop() -> str:
        """Stop recording; returns samples, duration, distance and the file name for record_fetch."""
        return dumps(await native("Record.stop"))

    @mcp.tool()
    async def record_fetch(name: str, dest_dir: str | None = None, plot: bool = True) -> str:
        """Copy a recording from the test PC to this machine (scp via the host config) and, by default,
        write a top-down SVG plot next to it. Returns local paths and a summary (distance, duration, stuck)."""
        from .. import trajectory

        listing = await native("Record.list")
        match = next((r for r in listing["recordings"] if r["name"] == name or r["file"] == name), None)
        if match is None:
            raise GameError(f"no recording {name}; known: {[r['name'] for r in listing['recordings']]}")
        folder = Path(dest_dir or manager.config.get("recordingsDir") or "recordings").expanduser()
        local = await trajectory.fetch(manager.config, match["path"], folder / match["file"])
        samples = trajectory.load(local)
        result = {"file": str(local), "summary": trajectory.summarize(samples)}
        if plot and len(samples) >= 2:
            svg = local.with_suffix(".svg")
            svg.write_text(trajectory.plot_svg(samples, title=match["name"]), encoding="utf-8")
            result["plot"] = str(svg)
        return dumps(result)

    @mcp.tool()
    async def trajectory_plot(file: str, output: str | None = None, color_by: str = "speed",
                              route: str | None = None) -> str:
        """Plot a fetched local recording as a top-down SVG (north up) coloured by speed or frame time,
        with start/end and stuck markers. route: a local file with a nav_plan reply or navRoute events,
        drawn under the path as a dashed line. Returns the SVG path and a summary."""
        from .. import trajectory

        samples = trajectory.load(file)
        routes = trajectory.load_routes(route) if route else ()
        out = Path(output) if output else Path(file).with_suffix(".svg")
        out.write_text(trajectory.plot_svg(samples, color_by=color_by, title=Path(file).stem, routes=routes),
                       encoding="utf-8")
        return dumps({"plot": str(out), "summary": trajectory.summarize(samples)})

    @mcp.tool()
    async def record_replay_camera(name: str, speed: float | None = None, method: str | None = None) -> str:
        """Play a recording's camera path back as a flythrough (free camera by default; method "player"
        moves the player along it instead). Returns at once; the replayFinished event marks the end."""
        return dumps(await native("Record.replayCamera", name=name, speed=speed, method=method))

    return mcp


def load_config(path):
    path = path or os.environ.get("SKYRIM_USE_CONFIG")
    if not path:
        raise SystemExit("skyrim-use: pass --config <file.json> (or set SKYRIM_USE_CONFIG)")
    return json.loads(Path(path).expanduser().read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-use", description="MCP server for playtesting Skyrim.")
    parser.add_argument("--config", help="host config JSON, as used by skyrim-devtools-smoke")
    options = parser.parse_args(argv)
    manager = GameManager(load_config(options.config))
    build_server(manager).run(transport="stdio")
    return 0
