"""Client for the single-controller Skyrim DevTools RPC session (protocol 2).

Several calls may be in flight at once. A reader task routes replies to their callers by id and
server notifications (JSON-RPC messages without an id) to an event queue and callbacks. A
timed-out call raises RpcError(-32008) and leaves the session usable.
"""

import asyncio
import base64
import json
from pathlib import Path


PROTOCOL = 2
BUILD = "sse-steam-1-7-104"
IMAGE_SHA256 = "846efccf0c1374d71f892907f46549560f2fcb0a75cb87a3eed438baa0f1402f"
INVENTORY = "InventoryMenu"
CONSOLE = "Console"
MAX_MESSAGE = 16 * 1024 * 1024
NOT_READY = -32004
TIMEOUT = -32008


def _params(**values):
    """Drop unset optional parameters so the plugin's own defaults apply."""
    return {key: value for key, value in values.items() if value is not None}


class RpcError(RuntimeError):
    def __init__(self, code, message, data=None):
        super().__init__(f"RPC {code} · {message}")
        self.code = code
        self.data = data


class ConnectionLost(RuntimeError):
    pass


class Session:
    def __init__(self, socket, session_id, event_limit=10000):
        self.socket = socket
        self.session_id = session_id
        self.epoch = None
        self.events = asyncio.Queue(maxsize=event_limit)
        self.dropped_events = 0
        self._next_id = 0
        self._pending = {}
        self._callbacks = []
        self._seq = 0
        self._closed = None
        self._reader = asyncio.create_task(self._read())

    def on_event(self, callback):
        """Call callback(event) for every notification; event is {type, seq, epoch, data}."""
        self._callbacks.append(callback)

    def off_event(self, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def _read(self):
        try:
            async for text in self.socket:
                message = json.loads(text)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ConnectionLost("Unexpected message from the plugin")
                if "id" not in message:
                    self._notify(message)
                    continue
                future = self._pending.pop(message["id"], None)
                if future is not None and not future.done():
                    future.set_result(message)
            self._closed = ConnectionLost("The plugin closed the connection")
        except Exception as error:
            self._closed = error if isinstance(error, ConnectionLost) else ConnectionLost(str(error))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._closed or ConnectionLost("Connection closed"))
            self._pending.clear()

    def _notify(self, message):
        if message.get("method") != "event" or not isinstance(message.get("params"), dict):
            return
        event = message["params"]
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
        for callback in list(self._callbacks):
            callback(event)

    async def call(self, method, timeout=None, **params):
        if self._closed is not None:
            raise self._closed
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.socket.send(json.dumps(dict(jsonrpc="2.0", id=request_id, method=method, params=params)))
            # The plugin enforces each method's own deadline; this only guards a dead link.
            async with asyncio.timeout(timeout or 240):
                response = await future
        finally:
            self._pending.pop(request_id, None)
        if ("result" in response) == ("error" in response):
            raise RuntimeError("Unexpected JSON-RPC response")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int:
                raise RuntimeError("Malformed JSON-RPC error")
            raise RpcError(error["code"], error.get("message", "Request failed"), error.get("data"))
        return response["result"]

    async def close(self):
        try:
            await self.call("Session.close", timeout=10)
        finally:
            self._reader.cancel()

    async def info(self):
        info = await self.call("Session.getInfo")
        if (
            info["protocol"] != PROTOCOL
            or info["build"] != BUILD
            or info["imageSha256"] != IMAGE_SHA256
            or info["sessionId"] != self.session_id
        ):
            raise RuntimeError("Protocol, image or session identity mismatch")
        return info

    async def ping(self):
        return await self.call("Session.ping")

    async def subscribe(self, *types):
        return await self.call("Events.subscribe", types=list(types))

    async def load_save(self, name, wait=True):
        result = await self.call("Session.loadSave", name=name, wait=wait)
        self.epoch = result["loadEpoch"]
        return result

    async def reload(self, wait=True):
        result = await self.call("Session.reload", wait=wait)
        self.epoch = result["loadEpoch"]
        return result

    async def snapshot(self):
        state = await self.call("Events.snapshot")
        if state["seq"] <= self._seq:
            raise RuntimeError("UI observation did not advance")
        self._seq = state["seq"]
        if state["loadCompleted"] and not state["loadSucceeded"]:
            raise RuntimeError("The game reported a failed save load")
        state["open"] = {menu["name"]: menu["ready"] for menu in state["menus"]}
        return state

    async def set_menu(self, menu, opening, wait=True):
        verb = "openMenu" if opening else "closeMenu"
        return await self.call(f"UI.{verb}", name=menu, wait=wait, loadEpoch=self.epoch)

    async def key(self, key, phase="tap", hold_ms=60):
        return await self.call("Input.key", key=key, phase=phase, holdMs=hold_ms)

    async def control(self, name, phase="tap", hold_ms=60):
        return await self.call("Input.control", name=name, phase=phase, holdMs=hold_ms)

    async def mouse(self, dx=0, dy=0):
        return await self.call("Input.mouse", dx=dx, dy=dy)

    async def console(self, command, target="selected"):
        return (await self.call("Console.execute", command=command, target=target))["lines"]

    async def center_on_cell(self, cell):
        return await self.call("World.centerOnCell", cell=cell, loadEpoch=self.epoch)

    async def capture(self, path=None, max_width=1024, format="png", quality=85, include_ui=True):
        """Grab the game's rendered frame. With a path, write the image there and return the result
        without its base64 payload; without one, the result keeps it in `data`."""
        result = await self.call("Screen.capture", maxWidth=max_width, format=format, quality=quality,
                                 includeUI=include_ui)
        if path is not None:
            Path(path).write_bytes(base64.b64decode(result.pop("data")))
        return result

    async def frame_stats(self, window=None):
        return await self.call("Perf.frameStats", **({} if window is None else {"window": window}))

    async def perf_reset(self):
        return await self.call("Perf.reset")

    async def perf_configure(self, hitch_ms=None, ring_size=None):
        params = {}
        if hitch_ms is not None:
            params["hitchMs"] = hitch_ms
        if ring_size is not None:
            params["ringSize"] = ring_size
        return await self.call("Perf.configure", **params)

    # Structured game-state queries. Results are compact JSON; form IDs are 8-digit hex strings.

    async def player_state(self, avs=None):
        return await self.call("Player.state", **({"avs": avs} if avs else {}))

    async def inventory(self, filter=None, limit=100):
        return await self.call("Player.inventory", **_params(filter=filter, limit=limit))

    async def equipped(self):
        return await self.call("Player.equipped")

    async def spells(self, kind=None, limit=100):
        return await self.call("Player.spells", **_params(kind=kind, limit=limit))

    async def nearby(self, radius=1000, types=None, limit=20):
        return await self.call("World.nearby", **_params(radius=radius, types=types, limit=limit))

    async def crosshair(self):
        return (await self.call("World.crosshair"))["ref"]

    async def form(self, form_id=None, editor_id=None):
        return await self.call("Form.get", **_params(formID=form_id, editorID=editor_id))

    async def ref(self, ref_id):
        return await self.call("Ref.get", refID=ref_id)

    async def quest(self, editor_id=None, form_id=None, all_objectives=False):
        return await self.call("Quest.get", **_params(
            editorID=editor_id, formID=form_id, allObjectives=all_objectives or None))

    async def active_quests(self, limit=30):
        return await self.call("Quest.active", limit=limit)

    async def global_value(self, editor_id=None, form_id=None):
        return (await self.call("Global.get", **_params(editorID=editor_id, formID=form_id)))["value"]

    async def game_time(self):
        return await self.call("Game.time")

    async def set_timescale(self, value):
        return await self.call("Game.setTimescale", value=value)

    async def teleport(self, x=None, y=None, z=None, ref_id=None, yaw_deg=None):
        return await self.call("Player.teleport", **_params(
            x=x, y=y, z=z, refID=ref_id, yawDeg=yaw_deg, loadEpoch=self.epoch))

    async def set_av(self, av, value):
        return await self.call("Player.setAV", av=av, value=value, loadEpoch=self.epoch)

    async def add_item(self, form_id, count=1):
        return await self.call("Player.addItem", formID=form_id, count=count, loadEpoch=self.epoch)

    async def remove_item(self, form_id, count=1):
        return await self.call("Player.removeItem", formID=form_id, count=count, loadEpoch=self.epoch)

    # --- Menus by meaning (see docs/protocol.md, "Menu semantics") ---

    async def ui_get(self, menu, path, depth=None):
        return await self.call("UI.getVariable", **_params(menu=menu, path=path, depth=depth))

    async def ui_set(self, menu, path, value):
        return await self.call("UI.setVariable", menu=menu, path=path, value=value)

    async def ui_invoke(self, menu, path, args=None, depth=None):
        return await self.call("UI.invoke", **_params(menu=menu, path=path, args=args or [], depth=depth))

    async def ui_dump(self, menu, path=None, depth=None):
        return await self.call("UI.dump", **_params(menu=menu, path=path, depth=depth))

    async def ui_call_delegate(self, menu, name, args=None):
        return await self.call("UI.callDelegate", menu=menu, name=name, args=args or [])

    async def message_box(self):
        return await self.call("UI.messageBox")

    async def message_box_select(self, index=None, text=None):
        return await self.call("UI.messageBoxSelect", **_params(index=index, text=text))

    async def show_message_box(self, text, buttons=None):
        return await self.call("UI.showMessageBox", **_params(text=text, buttons=buttons))

    async def list_items(self, menu):
        return await self.call("UI.listItems", menu=menu)

    async def select_item(self, menu, index=None, text=None, action=None, count=None):
        return await self.call("UI.selectItem", **_params(menu=menu, index=index, text=text, action=action, count=count))

    async def ui_list(self, menu):
        return await self.call("UI.list", menu=menu)

    async def ui_select(self, menu, index=None, text=None, path=None, press=None):
        return await self.call("UI.select", **_params(menu=menu, index=index, text=text, path=path, press=press))

    async def notifications(self, since=0):
        return await self.call("UI.notifications", since=since)

    async def subtitles(self):
        return await self.call("UI.subtitles")

    async def dialogue_state(self):
        return await self.call("Dialogue.state")

    async def dialogue_select(self, index=None, text=None):
        return await self.call("Dialogue.select", **_params(index=index, text=text))

    async def dialogue_wait(self, timeout_ms=None, skip=None, since=None):
        return await self.call("Dialogue.waitIdle", timeout=((timeout_ms or 30000) / 1000) + 10,
                               **_params(timeoutMs=timeout_ms, skip=skip, since=since))

    async def dialogue_advance(self):
        return await self.call("Dialogue.advance")

    async def dialogue_close(self):
        return await self.call("Dialogue.close")

    async def dialogue_start(self, ref_id=None):
        return await self.call("Dialogue.start", **_params(refID=ref_id, loadEpoch=self.epoch))

    async def next_event(self, predicate, timeout):
        """Wait for a queued notification that satisfies predicate, discarding the others."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if predicate(event):
                    return event

    def watch(self, *types, limit=10000):
        """Async context manager and iterator over events of the given types; see logs.EventWatch."""
        from .logs import EventWatch

        return EventWatch(self, types, limit)

    def papyrus_watch(self, **options):
        """Collects Papyrus log lines written inside the block; see logs.PapyrusWatch."""
        from .logs import PapyrusWatch

        return PapyrusWatch(self, **options)

    async def heartbeat(self, **options):
        """One main/UI-thread liveness reading (ok, busy, hang, unresponsive, lost); see logs.heartbeat."""
        from .logs import heartbeat

        return await heartbeat(self, **options)

    def monitor(self, **options):
        """Starts a background hang monitor; see logs.HangMonitor."""
        from .logs import HangMonitor

        return HangMonitor(self, **options)

    async def papyrus_log(self, since=0, level="info", limit=500, contains=""):
        return await self.call("Papyrus.log", since=since, level=level, limit=limit, contains=contains)

    async def papyrus_stats(self):
        return await self.call("Papyrus.stats")

    async def tail_log(self, source="papyrus", lines=100):
        return await self.call("Logs.tail", source=source, lines=lines)

    def drain_events(self):
        drained = []
        while not self.events.empty():
            drained.append(self.events.get_nowait())
        return drained


def _params(**values):
    """Drops unset optional parameters so the plugin applies its own defaults."""
    return {key: value for key, value in values.items() if value is not None}


async def observe(read, predicate, label, timeout):
    """Poll observations so a timed-out mutation is never submitted twice."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            state = await read()
        except RpcError as error:
            # Early in startup the UI cannot answer yet; that is a state to wait out, not a failure.
            if error.code != NOT_READY:
                raise
        else:
            if predicate(state):
                return state
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"{label} timed out")
        await asyncio.sleep(0.1)
