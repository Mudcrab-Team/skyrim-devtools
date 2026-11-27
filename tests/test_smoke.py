import asyncio
import base64
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from skyrim_devtools.client import BUILD, IMAGE_SHA256, INVENTORY, TIMEOUT, ConnectionLost, RpcError, Session
from skyrim_devtools.smoke import Counts, LogonInactive, exercise, run


SESSION_ID = "0" * 32
CAPABILITIES = ["loadSave", "events", "menus", "input", "console", "headless"]


class ToySocket:
    """Protocol 2 plugin stand-in: replies and notifications share one ordered stream."""

    def __init__(self, events=None):
        self.events = [] if events is None else events
        self.methods = []
        self.inbox = asyncio.Queue()
        self.epoch = 0
        self.seq = 0
        self.load_ok = True
        self.menus = {"Main Menu": True}
        self.wrong_image = False
        self.reject = {}
        self.delay = {}
        self.startup_reads = 0
        self.tab_closes = True
        self.closed = False

    def push(self, message):
        self.inbox.put_nowait(json.dumps(message))

    def emit(self, kind, data):
        self.push({"jsonrpc": "2.0", "method": "event",
                   "params": {"type": kind, "seq": 0, "epoch": self.epoch, "data": data}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        text = await self.inbox.get()
        if text is None:
            raise StopAsyncIteration
        return text

    async def send(self, text):
        request = json.loads(text)
        method, params = request["method"], request["params"]
        self.methods.append((method, params))
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if method in self.reject:
            reply["error"] = {"code": self.reject[method], "message": "Toy failure", "data": {"method": method}}
        else:
            try:
                reply["result"] = self.handle(method, params)
            except RpcError as error:
                reply["error"] = {"code": error.code, "message": "Toy failure"}
        if method in self.delay:
            asyncio.get_running_loop().call_later(self.delay[method], self.push, reply)
        else:
            self.push(reply)

    def handle(self, method, params):
        if method == "Session.getInfo":
            return {"protocol": 2, "build": BUILD, "imageSha256": "wrong" if self.wrong_image else IMAGE_SHA256,
                    "sessionId": SESSION_ID, "dataLoaded": True, "headless": True, "capabilities": CAPABILITIES}
        if method in {"Session.ping", "Events.subscribe"}:
            return {}
        if method == "Events.snapshot":
            if self.startup_reads:
                self.startup_reads -= 1
                raise RpcError(-32004, "not ready")
            self.seq += 1
            state = {"seq": self.seq, "loadEpoch": self.epoch, "loadCompleted": bool(self.epoch),
                     "loadSucceeded": bool(self.epoch and self.load_ok), "playable": bool(self.epoch and self.load_ok),
                     "foreground": False, "menus": [{"name": n, "ready": r} for n, r in self.menus.items()]}
            self.menus = {name: True for name in self.menus}
            return state
        if method in {"Session.loadSave", "Session.reload"}:
            self.epoch += 1
            self.menus = {}
            self.emit("loadGame", {"success": self.load_ok, "loadEpoch": self.epoch})
            return {"loadEpoch": self.epoch}
        if method == "Console.execute":
            lines = {"GetSKSEVersion": ["SKSE64 version: 2.3.1, release idx 72, runtime 01070680"],
                     "GetAV Health": ["GetActorValue: Health >> 100.00"]}
            return {"command": params["command"], "lines": lines[params["command"]]}
        if params.get("loadEpoch") != self.epoch:
            raise AssertionError("Mutation used the wrong load epoch")
        if method == "UI.openMenu":
            self.menus[params["name"]] = False
            self.emit("menuOpenClose", {"menu": params["name"], "opening": True})
            return {"menu": params["name"], "open": True}
        if method == "UI.closeMenu" and params["name"] in self.menus:
            del self.menus[params["name"]]
            self.emit("menuOpenClose", {"menu": params["name"], "opening": False})
            return {"menu": params["name"], "open": False}
        raise AssertionError(f"Unexpected request {method}")

    async def close(self):
        self.events.append("socket-close")
        self.inbox.put_nowait(None)


class InputSocket(ToySocket):
    def handle(self, method, params):
        if method == "Input.key":
            assert params["key"] == "Tab" and params["phase"] == "tap"
            if self.tab_closes and self.menus.get(INVENTORY):
                del self.menus[INVENTORY]
            return {"device": "keyboard", "id": 15, "poll": 1}
        return super().handle(method, params)


class ScreenSocket(InputSocket):
    PNG = b"\x89PNG\r\n\x1a\n toy"

    def handle(self, method, params):
        if method == "Screen.capture":
            assert params["maxWidth"] == 1024 and params["includeUI"] is True
            return {"frame": 7, "width": 1024, "height": 576, "format": "png",
                    "data": base64.b64encode(self.PNG).decode()}
        if method == "Perf.frameStats":
            return {"count": 600, "meanMs": 16.7, "fps": 59.9, "window": params.get("window")}
        return super().handle(method, params)


class ToyHost:
    local_port = 48681

    def __init__(self, start_fails=False, stop_phase="stopped"):
        self.events = []
        self.start_fails = start_fails
        self.stop_phase = stop_phase
        self.returncode = None
        self.logon = {"active": True, "state": "Active", "account": "tester"}

    async def command(self, action, session_id="", mode=""):
        if action == "logon":
            if isinstance(self.logon, Exception):
                raise self.logon
            return dict(self.logon)
        self.events.append(action)
        return {"sessionId": SESSION_ID, "port": 48682, "token": "a" * 64}

    async def wait_ready(self, session_id):
        assert session_id == SESSION_ID
        self.events.append("ready")
        if self.start_fails:
            raise RuntimeError("Toy preflight rejected the launch")

    async def tunnel(self, port):
        assert port == 48682
        self.events.append("tunnel")
        return self

    async def stop(self, session_id):
        assert session_id == SESSION_ID
        self.events.append("stop")
        return {"phase": self.stop_phase, "exitCode": 0, "reason": ""}

    def terminate(self):
        self.events.append("tunnel-stop")

    async def wait(self):
        self.returncode = 0


@patch("skyrim_devtools.smoke.MENU_SETTLE", 0)
class DevToolsSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_scenario_counts_events_tab_console_and_reload(self):
        socket = InputSocket()
        counts = Counts()
        timings = {}
        await exercise(Session(socket, SESSION_ID), counts, 3, tab_trials=2, timings=timings)
        self.assertEqual(counts, Counts(sessions=1, loaded=1, inventory_cycles=3, events=6, tab_taps=2,
                                        tab_taps_unfocused=2, console_queries=2, reloads=1, headless=True))
        names = [method for method, _ in socket.methods]
        self.assertEqual(names.count("Session.loadSave"), 1)
        self.assertEqual(names.count("Input.key"), 2)
        self.assertEqual(names.count("Session.reload"), 1)
        self.assertIn("reloadSeconds", timings)
        self.assertNotIn(INVENTORY, socket.menus)

    async def test_screenshot_writes_both_views_and_samples_frame_times(self):
        socket = ScreenSocket()
        timings = {}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "shot.png"
            await exercise(Session(socket, SESSION_ID), Counts(), 1, tab_trials=1, timings=timings, screenshot=target)
            self.assertEqual(target.read_bytes(), ScreenSocket.PNG)
            self.assertEqual((Path(folder) / "shot-inventory.png").read_bytes(), ScreenSocket.PNG)
        self.assertNotIn("data", timings["screenshots"]["world"])
        self.assertEqual(timings["frameStats"]["window"], 10)

    async def test_load_failure_stops_before_menu_operations(self):
        socket = ToySocket()
        socket.load_ok = False
        counts = Counts()
        with self.assertRaisesRegex(RuntimeError, "failed save load"):
            await exercise(Session(socket, SESSION_ID), counts, 3)
        self.assertEqual(counts, Counts(sessions=1, headless=True))
        self.assertFalse(any(method.startswith("UI.") for method, _ in socket.methods))

    async def test_snapshot_waits_for_startup_ui(self):
        socket = ToySocket()
        socket.startup_reads = 2
        await exercise(Session(socket, SESSION_ID), Counts(), 3, session_only=True)
        names = [method for method, _ in socket.methods]
        self.assertEqual(names[:5], ["Session.getInfo", "Events.subscribe"] + ["Events.snapshot"] * 3)
        self.assertIn("Session.loadSave", names)

    async def test_identity_mismatch_prevents_mutations(self):
        socket = ToySocket()
        socket.wrong_image = True
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            await exercise(Session(socket, SESSION_ID), Counts(), 3)
        self.assertEqual(socket.methods, [("Session.getInfo", {})])

    async def test_timeout_does_not_poison_the_session(self):
        socket = ToySocket()
        socket.reject = {"Events.snapshot": TIMEOUT}
        session = Session(socket, SESSION_ID)
        with self.assertRaises(RpcError) as caught:
            await session.snapshot()
        self.assertEqual(caught.exception.code, TIMEOUT)
        self.assertEqual(caught.exception.data, {"method": "Events.snapshot"})
        self.assertEqual((await session.info())["protocol"], 2)

    async def test_concurrent_calls_resolve_out_of_order(self):
        socket = ToySocket()
        socket.delay = {"Session.getInfo": 0.05}
        session = Session(socket, SESSION_ID)
        order = []

        async def track(name, call):
            await call
            order.append(name)

        await asyncio.gather(track("info", session.info()), track("ping", session.ping()))
        self.assertEqual(order, ["ping", "info"])

    async def test_notifications_reach_queue_and_callbacks(self):
        socket = ToySocket()
        session = Session(socket, SESSION_ID)
        seen = []
        session.on_event(seen.append)
        socket.emit("cellChange", {"formId": "0000003c", "entered": True})
        event = await session.next_event(lambda e: e["type"] == "cellChange", 1)
        self.assertEqual(event["data"]["formId"], "0000003c")
        self.assertEqual([e["type"] for e in seen], ["cellChange"])

    async def test_late_reply_after_client_timeout_is_ignored(self):
        socket = ToySocket()
        socket.delay = {"Session.ping": 0.1}
        session = Session(socket, SESSION_ID)
        with self.assertRaises(TimeoutError):
            await session.call("Session.ping", timeout=0.01)
        await asyncio.sleep(0.15)
        self.assertEqual((await session.info())["protocol"], 2)

    async def test_closed_connection_fails_pending_and_later_calls(self):
        socket = ToySocket()
        socket.delay = {"Session.ping": 10}
        session = Session(socket, SESSION_ID)
        pending = asyncio.create_task(session.ping())
        await asyncio.sleep(0)
        await socket.close()
        with self.assertRaises(ConnectionLost):
            await pending
        with self.assertRaises(ConnectionLost):
            await session.info()

    async def test_tab_failure_is_counted_and_inventory_closed_for_shutdown(self):
        socket = InputSocket()
        socket.tab_closes = False
        counts = Counts()

        async def observe(read, predicate, label, timeout):
            for _ in range(3):
                state = await read()
                if predicate(state):
                    return state
            raise TimeoutError(f"{label} timed out")

        with patch("skyrim_devtools.smoke.observe", observe):
            with self.assertRaisesRegex(RuntimeError, "Tab closed the inventory 0/2"):
                await exercise(Session(socket, SESSION_ID), counts, 1, tab_trials=2)
        self.assertEqual(counts.tab_failures, 2)
        self.assertNotIn(INVENTORY, socket.menus)

    async def test_session_only_stops_before_closing_transport(self):
        host = ToyHost()
        socket = ToySocket(host.events)

        async def connect(uri, **kwargs):
            self.assertEqual(uri, "ws://127.0.0.1:48681/rpc")
            self.assertEqual(kwargs["additional_headers"], {"Authorization": "Bearer " + "a" * 64})
            self.assertEqual(kwargs["max_size"], 16 * 1024 * 1024)
            return socket

        counts = Counts()
        await run(host, connect, counts, 3, session_only=True)
        self.assertEqual(counts, Counts(sessions=1, loaded=1, clean_stops=1, headless=True))
        self.assertEqual(host.events, ["start", "ready", "tunnel", "stop", "socket-close", "tunnel-stop"])

    async def test_startup_failure_still_requests_stop(self):
        host = ToyHost(start_fails=True, stop_phase="failed")

        async def connect(*args, **kwargs):
            self.fail("Refused startup must never connect")

        with self.assertRaisesRegex(RuntimeError, "Toy preflight"):
            await run(host, connect, Counts(), 3, session_only=True)
        self.assertEqual(host.events, ["start", "ready", "stop"])

    async def test_disconnected_logon_session_refuses_before_start(self):
        host = ToyHost()
        host.logon = {"active": False, "state": "Disc", "account": "tester"}

        async def connect(*args, **kwargs):
            self.fail("A refused launch must never connect")

        with self.assertRaisesRegex(LogonInactive, "logon session is not active \\(state Disc\\)"):
            await run(host, connect, Counts(), 3, session_only=True)
        self.assertEqual(host.events, [])

    async def test_host_without_logon_action_still_starts(self):
        host = ToyHost(start_fails=True, stop_phase="failed")
        host.logon = RuntimeError("Windows host logon failed")

        async def connect(*args, **kwargs):
            self.fail("Refused startup must never connect")

        with self.assertRaisesRegex(RuntimeError, "Toy preflight"):
            await run(host, connect, Counts(), 3, session_only=True)
        self.assertEqual(host.events, ["start", "ready", "stop"])

    async def test_crash_is_reported_after_socket_and_tunnel_cleanup(self):
        host = ToyHost(stop_phase="crashed")
        socket = ToySocket(host.events)

        async def connect(*args, **kwargs):
            return socket

        counts = Counts()
        with self.assertRaisesRegex(RuntimeError, "did not stop cleanly .*crashed"):
            await run(host, connect, counts, 3, session_only=True)
        self.assertEqual(counts, Counts(sessions=1, loaded=1, headless=True))
        self.assertEqual(host.events[-3:], ["stop", "socket-close", "tunnel-stop"])

    async def test_shutdown_failure_preserves_scenario_failure(self):
        host = ToyHost(stop_phase="crashed")
        socket = ToySocket(host.events)
        socket.reject = {"Session.getInfo": TIMEOUT}

        async def connect(*args, **kwargs):
            return socket

        with self.assertRaisesRegex(RuntimeError, r"RPC -32008[^\n]*\nShutdown also failed .*crashed"):
            await run(host, connect, Counts(), 3, session_only=False)
        self.assertEqual(host.events[-3:], ["stop", "socket-close", "tunnel-stop"])


if __name__ == "__main__":
    unittest.main()
