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

from .manager import GameError, GameManager

INSTRUCTIONS = """\
Playtest Skyrim SE (offline test profile) through an injected plugin. Call game_start first (headless by
default, about 30-90 s), then observe with snapshot, act with console / press_key / open_menu / look, and
verify with state tools. Time rules: the game ignores all input while the load fade is showing and a menu
ignores input for about 0.3 s after it opens; use wait_for instead of fixed sleeps. Call game_stop when done.
Anything the plugin can do that has no curated tool is reachable via rpc_methods + rpc_call."""

# Curated wrappers for native methods that older plugin builds lack.
UNAVAILABLE = "method {} is not available in this plugin build (see rpc_methods); update the plugin or use another tool"


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


def build_server(manager):
    @contextlib.asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            # a game left running would hold the single test PC after the client is gone
            with contextlib.suppress(Exception):
                await manager.stop()

    mcp = FastMCP("skyrim-use", instructions=INSTRUCTIONS, lifespan=lifespan)

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
        by game_status/any tool; call game_start again to relaunch."""
        return dumps(await manager.start(headless=headless, save=save))

    @mcp.tool()
    async def game_stop() -> str:
        """Close the game and release the connection. Always call this when finished."""
        return dumps(await manager.stop())

    @mcp.tool()
    async def game_status() -> str:
        """Phase (idle/starting/running/crashed/stopped/failed), session info and the crash reason if the game
        died. Probes the live link, so it notices a crash that happened while you were idle."""
        return dumps(await manager.status())

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
        Ignored during load fades; menus ignore it for ~0.3 s after opening."""
        return dumps(await manager.call("Input.key", key=key, phase=phase, holdMs=hold_ms))

    @mcp.tool()
    async def press_control(name: str, phase: str = "tap", hold_ms: int = 60) -> str:
        """Fire a game control by user-event name (Activate, Journal, Tween Menu, Jump, Sneak, Forward,
        Ready Weapon ...), resolved to whatever key is bound in the current input context. Preferred over
        press_key because it works inside menus too. hold_ms 0-10000."""
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
        units is 1 m); types filters by reference kind (e.g. ["Actor", "Door"]); limit 1-200 (default 20)."""
        return dumps(await native("World.nearby", radius=radius, types=types, limit=limit))

    @mcp.tool()
    async def world_crosshair() -> str:
        """What the crosshair points at (World.crosshair): the reference and its base form, if any."""
        return dumps(await native("World.crosshair"))

    @mcp.tool()
    async def quest_get(editor_id: str | None = None, form_id: str | None = None, all_objectives: bool = False) -> str:
        """One quest's stage, running/completed flags and objectives (Quest.get); give editor_id (e.g.
        MQ101) or form_id (hex). all_objectives includes finished and hidden ones."""
        return dumps(await native("Quest.get", editorID=editor_id, formID=form_id,
                                  allObjectives=True if all_objectives else None))

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
        """Current conversation: speaker, spoken line and selectable options (Dialogue.state)."""
        return dumps(await native("Dialogue.state"))

    @mcp.tool()
    async def dialogue_select(index: int | None = None, text: str | None = None) -> str:
        """Choose a dialogue option by 0-based index from dialogue_state, or by its text (Dialogue.select)."""
        return dumps(await native("Dialogue.select", index=index, text=text))

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
    async def papyrus_log(since: int | None = None, level: str | None = None, limit: int | None = None,
                          contains: str | None = None) -> str:
        """Papyrus log lines (Papyrus.log): script errors, warnings and Debug.Trace output. Pass the last
        returned sequence as `since` to read only new lines; level is the minimum severity (default info);
        contains filters by substring; limit default 500."""
        return dumps(await native("Papyrus.log", since=since, level=level, limit=limit, contains=contains))

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
