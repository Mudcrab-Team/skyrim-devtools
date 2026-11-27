import asyncio
import base64
import json
import tempfile
import unittest

from skyrim_devtools.use.manager import GameError, GameManager
from skyrim_devtools.use.perception import crop_rect, zoomed_fov
from skyrim_devtools.use.server import build_server, dig, perf_verdict
from skyrim_devtools.use.timeline import Recorder

SESSION = "a" * 32
TOKEN = "b" * 64
METHODS = ["Console.execute", "Events.list", "Events.snapshot", "Events.subscribe", "Events.unsubscribe",
           "Input.key", "Session.close", "Session.getInfo", "Session.listSaves", "Session.loadSave",
           "Session.ping", "UI.closeMenu", "UI.listMenus", "UI.openMenu", "Screen.capture", "Player.state",
           "World.inspect", "Screen.project", "Camera.lookAt", "Camera.state", "Camera.set", "Setting.set",
           "Setting.get"]


class FakeSocket:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.calls = []
        self.menus = {"Main Menu"}
        self.playable = False
        self.epoch = 0
        self.dead = False
        self.closed = False
        self.results = {}
        self.errors = {}

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push(self, message):
        self.inbox.put_nowait(json.dumps(message))

    def kill(self):
        self.dead = True
        self.inbox.put_nowait(None)

    async def close(self):
        self.closed = True
        self.inbox.put_nowait(None)

    async def send(self, text):
        if self.dead:
            raise OSError("link down")
        request = json.loads(text)
        method, params = request["method"], request["params"]
        self.calls.append((method, params))
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if method in self.errors:
            reply["error"] = self.errors[method]
        elif method in self.results:
            reply["result"] = self.results[method]
        elif method == "Session.getInfo":
            reply["result"] = {"protocol": 2, "dataLoaded": True, "loadEpoch": self.epoch, "headless": True,
                               "capabilities": ["input"], "methods": [{"name": m, "thread": "any", "timeoutMs": 1} for m in METHODS]}
        elif method == "Events.list":
            reply["result"] = [{"type": "menuOpenClose"}, {"type": "console"}, {"type": "loadGame"}]
        elif method == "Events.snapshot":
            reply["result"] = {"seq": len(self.calls), "loadEpoch": self.epoch, "playable": self.playable,
                               "menus": [{"name": m, "ready": True} for m in sorted(self.menus)], "foreground": False,
                               "loadCompleted": self.playable, "loadSucceeded": True, "lastSave": None}
        elif method == "Session.loadSave":
            self.epoch += 1
            self.playable = True
            self.menus = set()
            reply["result"] = {"loadEpoch": self.epoch}
        elif method == "UI.openMenu":
            self.menus.add(params["name"])
            self.push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "menuOpenClose", "seq": 1, "epoch": self.epoch, "data": {"menu": params["name"], "opening": True}}})
            reply["result"] = {"menu": params["name"], "open": True}
        elif method == "UI.listMenus":
            reply["result"] = [{"name": m, "open": m in self.menus} for m in ("Main Menu", "InventoryMenu")]
        elif method == "Console.execute":
            reply["result"] = {"command": params["command"], "lines": ["line one", "line two"]}
        elif method == "Session.ping":
            reply["result"] = {"pong": True}
        elif method == "Player.state":
            reply["result"] = {"location": {"cell": "Fixture"}}
        else:
            reply["result"] = {}
        self.push(reply)


class FakeTunnel:
    returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return 0


class FakeHost:
    local_port = 1

    def __init__(self):
        self.status = {"phase": "running", "reason": "", "exitCode": None}
        self.commands = []
        self.stopped = False
        self.stop_result = {"phase": "stopped", "exitCode": 0, "reason": ""}
        self.crash_logs = []
        self.grace = None
        self.health = {"running": False}
        self.stale = []
        self.logon = {"active": True, "state": "Active", "account": "tester"}

    async def command(self, action, session_id="", mode="", grace=None, force=False):
        if action == "logon":
            return dict(self.logon)
        self.commands.append((action, mode))
        if action == "start":
            started = {"sessionId": SESSION, "token": TOKEN, "port": 4000}
            if self.stale:
                started["staleCleared"] = list(self.stale)
            return started
        if action == "health":
            if isinstance(self.health, Exception):
                raise self.health
            return dict(self.health)
        if action == "crashes":
            return list(self.crash_logs)
        if action == "evidence":
            return {"crashLogs": list(self.crash_logs)}
        return dict(self.status)

    async def wait_ready(self, session_id):
        return self.status

    async def stop(self, session_id, grace=12):
        self.stopped = True
        self.grace = grace
        return dict(self.stop_result)

    async def tunnel(self, port):
        return FakeTunnel()


class Harness:
    def __init__(self, keepalive=60):
        self.host = FakeHost()
        self.sockets = []

        async def connect(port, token):
            socket = FakeSocket()
            if self.sockets:
                socket.playable = self.sockets[-1].playable
                socket.menus = set(self.sockets[-1].menus)
            self.sockets.append(socket)
            return socket

        self.manager = GameManager({}, host=self.host, connect=connect, keepalive=keepalive)
        self.timelines = tempfile.TemporaryDirectory()
        self.recorder = Recorder(self.timelines.name)
        self.server = build_server(self.manager, self.recorder)

    async def tool(self, tool_name, **args):
        result = await self.server.call_tool(tool_name, args)
        return result[0] if isinstance(result, tuple) else result

    async def text(self, tool_name, **args):
        content = await self.tool(tool_name, **args)
        return content[0].text


def run(coro):
    return asyncio.run(coro)


class UseTests(unittest.TestCase):
    def test_start_snapshot_console_stop(self):
        async def scenario():
            h = Harness()
            started = json.loads(await h.text("game_start", save="Fixture"))
            self.assertEqual(started["phase"], "running")
            self.assertEqual(h.host.commands[0], ("start", "headless"))
            self.assertIn(("Session.loadSave", {"name": "Fixture", "wait": True}), h.sockets[0].calls)
            subscribed = [c for c in h.sockets[0].calls if c[0] == "Events.subscribe"][0][1]["types"]
            self.assertEqual(subscribed, ["menuOpenClose", "loadGame"])
            snap = json.loads(await h.text("snapshot"))
            self.assertTrue(snap["playable"])
            self.assertEqual(await h.text("console", command="GetAV Health", target="player"), "line one\nline two")
            stopped = json.loads(await h.text("game_stop"))
            self.assertEqual(stopped["phase"], "stopped")
            self.assertTrue(h.host.stopped)
            with self.assertRaises(Exception):
                await h.text("snapshot")
        run(scenario())

    def test_start_refused_when_logon_session_is_disconnected(self):
        async def scenario():
            h = Harness()
            h.host.logon = {"active": False, "state": "Disc", "account": "tester"}
            with self.assertRaises(Exception) as caught:
                await h.text("game_start")
            self.assertIn("logon session is not active (state Disc)", str(caught.exception))
            self.assertNotIn(("start", "headless"), h.host.commands)
            self.assertEqual(h.sockets, [])
        run(scenario())

    def test_start_is_idempotent(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            again = json.loads(await h.text("game_start"))
            self.assertIn("already running", again["note"])
            self.assertEqual(len(h.sockets), 1)
            await h.text("game_stop")
        run(scenario())

    def test_events_drain_and_wait_for(self):
        async def scenario():
            h = Harness()
            await h.text("game_start", save="Fixture")
            await h.text("open_menu", name="InventoryMenu")
            waited = json.loads(await h.text("wait_for", menu_open="InventoryMenu", event_type="menuOpenClose", timeout_s=2))
            self.assertTrue(waited["ok"])
            drained = json.loads(await h.text("events_drain"))
            self.assertEqual(drained["events"][0]["data"], {"menu": "InventoryMenu", "opening": True})
            self.assertEqual(json.loads(await h.text("events_drain"))["events"], [])
            timeout = json.loads(await h.text("wait_for", event_type="cellChange", timeout_s=0.3, poll_ms=50))
            self.assertFalse(timeout["ok"])
            probe = json.loads(await h.text("wait_for", method="Player.state", path="location.cell", equals="Fixture", timeout_s=1))
            self.assertTrue(probe["ok"])
            await h.text("game_stop")
        run(scenario())

    def test_missing_native_method_degrades(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            with self.assertRaises(Exception) as caught:
                await h.text("quest_get", editor_id="MQ101")
            self.assertIn("not available in this plugin build", str(caught.exception))
            self.assertEqual(json.loads(await h.text("player_state")), {"location": {"cell": "Fixture"}})
            await h.text("game_stop")
        run(scenario())

    def test_stale_plugin_is_reported(self):
        async def scenario():
            h = Harness()
            started = json.loads(await h.text("game_start"))
            plugin = started["plugin"]
            self.assertTrue(plugin["stale"])
            self.assertIsNone(plugin["buildId"])
            self.assertIn("Quest.get", plugin["missingMethods"])
            self.assertIn("older than", started["warning"])
            self.assertIn("older than", json.loads(await h.text("game_status"))["warning"])
            await h.text("game_stop")
        run(scenario())

    def test_current_plugin_reports_build_id(self):
        from skyrim_devtools.use.manager import curated_methods, plugin_report

        names = curated_methods()
        self.assertIn("Ref.activate", names)
        self.assertIn("Dialogue.waitIdle", names)
        report = plugin_report({"pluginBuild": "abc:123"}, {name: {} for name in names})
        self.assertEqual(report, {"buildId": "abc:123", "missingMethods": [], "stale": False})

    def test_screenshot_returns_image(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Screen.capture"] = {"format": "png", "width": 1, "height": 1,
                                                        "data": base64.b64encode(b"\x89PNG-fake").decode()}
            content = await h.tool("screenshot")
            self.assertEqual(content[0].type, "image")
            self.assertEqual(len(content), 2)
            self.assertEqual(base64.b64decode(content[0].data), b"\x89PNG-fake")
            await h.text("game_stop")
        run(scenario())

    def test_rpc_call_and_errors(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            methods = json.loads(await h.text("rpc_methods"))
            self.assertIn("Player.state", [m["name"] for m in methods])
            result = json.loads(await h.text("rpc_call", method="Session.ping", params={}))
            self.assertEqual(result, {"pong": True})
            await h.text("game_stop")
        run(scenario())

    def test_crash_is_reported_with_host_reason(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.host.status = {"phase": "failed", "reason": "process exited", "exitCode": -1}
            h.sockets[0].kill()
            await asyncio.sleep(0.05)
            with self.assertRaises(Exception) as caught:
                await h.text("snapshot")
            self.assertIn("process exited", str(caught.exception))
            status = json.loads(await h.text("game_status"))
            self.assertEqual(status["phase"], "crashed")
            self.assertEqual(status["crash"]["exitCode"], -1)
            restarted = json.loads(await h.text("game_start"))
            self.assertEqual(restarted["phase"], "running")
            await h.text("game_stop")
        run(scenario())

    def test_reconnects_when_game_survives_link_loss(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].kill()
            await asyncio.sleep(0.05)
            snap = json.loads(await h.text("snapshot"))
            self.assertIn("playable", snap)
            self.assertEqual(len(h.sockets), 2)
            await h.text("game_stop")
        run(scenario())

    def test_keepalive_detects_crash(self):
        async def scenario():
            h = Harness(keepalive=0.05)
            await h.text("game_start")
            h.host.status = {"phase": "failed", "reason": "gone", "exitCode": 3}
            h.sockets[0].dead = True
            await asyncio.sleep(0.4)
            self.assertEqual(h.manager.phase, "crashed")
            await h.manager.stop()
        run(scenario())

    def test_stop_reports_the_path_taken(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            stopped = json.loads(await h.text("game_stop", grace_seconds=30))
            self.assertEqual((stopped["path"], stopped["cleanExit"], h.host.grace), ("graceful", True, 30))
            await h.text("game_start")
            h.host.stop_result = {"phase": "forced", "exitCode": None, "reason": "requested", "stopPath": "killed-after-5s"}
            stopped = json.loads(await h.text("game_stop", grace_seconds=5))
            self.assertEqual((stopped["path"], stopped["cleanExit"]), ("killed-after-5s", False))
        run(scenario())

    def test_crash_on_quit_carries_the_log(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.host.crash_logs = [{"name": "crash-x.log", "exception": 'Unhandled exception "EXCEPTION_ACCESS_VIOLATION"\tcall [rax]',
                                  "topFrames": ["0 SkyrimSE.exe+1193E59 -> 87837+0x59"]}]
            h.host.stop_result = {"phase": "crashed", "exitCode": 1, "reason": "nonzero-exit", "stopPath": "closing-crashed",
                                  "evidence": "evidence"}
            stopped = json.loads(await h.text("game_stop"))
            self.assertEqual(stopped["path"], "closing-crashed")
            self.assertEqual(stopped["crash"]["log"]["topFrames"][0], "0 SkyrimSE.exe+1193E59 -> 87837+0x59")
            self.assertNotIn("\t", stopped["crash"]["log"]["exception"])
        run(scenario())

    def test_status_reports_crash_log_after_a_silent_crash(self):
        async def scenario():
            h = Harness(keepalive=0.05)
            await h.text("game_start")
            h.host.status = {"phase": "crashed", "reason": "nonzero-exit", "exitCode": 1, "evidence": "evidence"}
            h.host.crash_logs = [{"name": "crash-y.log", "exception": "Unhandled exception", "topFrames": ["0 SkyrimSE.exe+1"]}]
            h.sockets[0].dead = True
            await asyncio.sleep(0.4)
            status = json.loads(await h.text("game_status"))
            self.assertEqual(status["phase"], "crashed")
            self.assertEqual(status["crash"]["log"]["file"], "crash-y.log")
            await h.manager.stop()
        run(scenario())

    def test_start_is_repeated_when_the_engine_dies_at_launch(self):
        async def scenario():
            h = Harness()
            real = h.host.wait_ready
            calls = []

            async def flaky(session_id):
                calls.append(session_id)
                if len(calls) == 1:
                    h.host.status = {"phase": "crashed", "reason": "nonzero-exit", "exitCode": 1}
                    raise RuntimeError("Windows host ended during startup (crashed, nonzero-exit)")
                h.host.status = {"phase": "running", "reason": "", "exitCode": None}
                return await real(session_id)

            h.host.wait_ready = flaky
            started = json.loads(await h.text("game_start"))
            self.assertEqual(started["phase"], "running")
            self.assertEqual(len(started["startRetries"]), 1)
            self.assertEqual(started["startRetries"][0]["exitCode"], 1)
            await h.text("game_stop")
        run(scenario())

    def test_vanity_camera_is_switched_off_at_start(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            calls = [c for c in h.sockets[0].calls if c[0] == "Setting.set"]
            self.assertEqual(calls, [("Setting.set", {"name": "fAutoVanityModeDelay:Camera", "value": 86400.0})])
            await h.text("game_stop")
        run(scenario())

    def test_world_inspect_passes_the_reference(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["World.inspect"] = {"refID": "000AA8CB", "openState": None, "angleDeg": {"z": 120.0}}
            text = await h.text("world_inspect", ref_id="000AA8CB")
            self.assertEqual(json.loads(text)["angleDeg"]["z"], 120.0)
            self.assertIn(("World.inspect", {"refID": "000AA8CB"}), h.sockets[0].calls)
            await h.text("game_stop")
        run(scenario())

    def test_screen_capture_zooms_crops_and_restores_fov(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.results["Camera.state"] = {"worldFov": 80.0}
            socket.results["Screen.project"] = {"center": [0.5, 0.5], "inFront": True, "onScreen": True,
                                                "rect": {"x": 0.4, "y": 0.45, "w": 0.2, "h": 0.1}}
            socket.results["Screen.capture"] = {"format": "png", "width": 2, "height": 2,
                                                "data": base64.b64encode(b"\x89PNG-fake").decode()}
            content = await h.tool("screen_capture", ref_id="000AA8CB", zoom=4)
            self.assertEqual(content[0].type, "image")
            methods = [c[0] for c in socket.calls]
            self.assertEqual(methods[-6:], ["Camera.lookAt", "Camera.state", "Camera.set", "Screen.project",
                                            "Screen.capture", "Camera.set"])
            fovs = [c[1]["fov"] for c in socket.calls if c[0] == "Camera.set"]
            self.assertEqual(fovs, [20.0, 80.0])
            capture = next(c[1] for c in socket.calls if c[0] == "Screen.capture")
            self.assertFalse(capture["includeUI"])
            self.assertAlmostEqual(capture["crop"]["w"], 0.26)
            await h.tool("screen_capture", region={"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}, include_ui=True)
            capture = [c[1] for c in socket.calls if c[0] == "Screen.capture"][-1]
            self.assertTrue(capture["includeUI"])
            self.assertEqual(capture["crop"], {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3})
            await h.text("game_stop")
        run(scenario())

    def test_crop_rect_and_zoom(self):
        rect = crop_rect({"center": [0.98, 0.5], "inFront": True, "rect": {"x": 0.96, "y": 0.49, "w": 0.04, "h": 0.02}})
        self.assertEqual(rect["w"], 0.06)
        self.assertAlmostEqual(rect["x"] + rect["w"], 1.0)
        self.assertIsNone(crop_rect({"inFront": False, "rect": {}}))
        self.assertEqual(zoomed_fov(80, 1), 80)
        self.assertEqual(zoomed_fov(80, 100), 5.0)

    def test_dig(self):
        self.assertEqual(dig({"a": [{"b": 5}]}, "a.0.b"), 5)
        self.assertIsNone(dig({"a": []}, "a.2"))


class PerfVerdictTests(unittest.TestCase):
    def test_smooth_session_is_ok(self):
        verdict = perf_verdict({"count": 600, "fps": 60.0, "p50Ms": 16.7, "p99Ms": 17.0, "maxMs": 20.0}, {"hitches": []}, 10)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["warnings"], [])

    def test_slow_session_warns(self):
        hitches = {"hitches": [{"ms": 900, "frame": 5}, {"ms": 300, "frame": 9}], "slowCalls": {"World.nearby": {"count": 1}}}
        verdict = perf_verdict({"count": 80, "fps": 8.0, "p50Ms": 16.7, "p99Ms": 900.0, "maxMs": 900.0}, hitches, 10)
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["warnings"]), 2)
        self.assertEqual(verdict["worstHitches"][0]["ms"], 900)

    def test_no_frames_warns(self):
        verdict = perf_verdict({"count": 0, "fps": None, "p99Ms": None}, {"hitches": []}, 10)
        self.assertFalse(verdict["ok"])


if __name__ == "__main__":
    unittest.main()
