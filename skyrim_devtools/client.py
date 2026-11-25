"""Client for the single-controller Skyrim DevTools RPC session (protocol 2).

Several calls may be in flight at once. A reader task routes replies to their callers by id and
server notifications (JSON-RPC messages without an id) to an event queue and callbacks. A
timed-out call raises RpcError(-32008) and leaves the session usable.
"""

import asyncio
import json


PROTOCOL = 2
BUILD = "sse-steam-1-7-104"
IMAGE_SHA256 = "846efccf0c1374d71f892907f46549560f2fcb0a75cb87a3eed438baa0f1402f"
INVENTORY = "InventoryMenu"
CONSOLE = "Console"
MAX_MESSAGE = 16 * 1024 * 1024
NOT_READY = -32004
TIMEOUT = -32008


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
        for callback in self._callbacks:
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

    async def next_event(self, predicate, timeout):
        """Wait for a queued notification that satisfies predicate, discarding the others."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if predicate(event):
                    return event

    def drain_events(self):
        drained = []
        while not self.events.empty():
            drained.append(self.events.get_nowait())
        return drained


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
