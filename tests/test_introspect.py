import asyncio
import json
import unittest

from skyrim_devtools.client import Session
from skyrim_devtools.use import introspect


class Recorder:
    def __init__(self):
        self.requests = []
        self.inbox = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        text = await self.inbox.get()
        if text is None:
            raise StopAsyncIteration
        return text

    async def send(self, text):
        request = json.loads(text)
        self.requests.append((request["method"], request["params"]))
        self.inbox.put_nowait(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}))


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def keep(function):
            self.tools[function.__name__] = function
            return function
        return keep


class ClientCalls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = Recorder()
        self.session = Session(self.socket, "0" * 32)

    async def asyncTearDown(self):
        await self.socket.inbox.put(None)

    async def test_requests_carry_only_the_given_fields(self):
        s = self.session
        await s.ui_find("Journal Menu", text="Brightness")
        await s.papyrus_classes(filter="quest", limit=5)
        await s.papyrus_functions("Game", kind="global")
        await s.papyrus_call("GetPlayer", "Game")
        await s.papyrus_call("GetDistance", "ObjectReference", args=["0x1A2B3"], form="0x14", timeout_ms=2000)
        await s.papyrus_call("ForceRefTo", args=["0x1A2B3"], alias={"quest": "MQ101", "id": 3})
        await s.forms_find(type="Weapon", name="iron", limit=3)
        await s.forms_info(editor_id="Gold001")
        await s.idles_list("0x1234", filter="wave")
        await s.idles_play("0x1234", "IdleWave")
        await s.idles_stop("0x1234")
        await s.camera_pose()
        await s.camera_mode("vanity")
        await s.camera_free(True, pos=[1, 2, 3], yaw_deg=90, pitch_deg=10)
        await s.camera_free(False)
        await s.camera_auto_vanity(False)
        await s.map_waypoint()
        await s.map_set_waypoint(100, 200)
        await s.map_clear_waypoint()
        await s.map_markers(contains="riv", visible=True)
        self.assertEqual(self.socket.requests, [
            ("UI.find", {"menu": "Journal Menu", "text": "Brightness"}),
            ("Papyrus.classes", {"filter": "quest", "limit": 5}),
            ("Papyrus.functions", {"class": "Game", "kind": "global"}),
            ("Papyrus.call", {"function": "GetPlayer", "args": [], "class": "Game"}),
            ("Papyrus.call", {"function": "GetDistance", "args": ["0x1A2B3"], "form": "0x14", "timeoutMs": 2000,
                              "class": "ObjectReference"}),
            ("Papyrus.call", {"function": "ForceRefTo", "args": ["0x1A2B3"], "alias": {"quest": "MQ101", "id": 3}}),
            ("Forms.find", {"type": "Weapon", "name": "iron", "limit": 3}),
            ("Forms.info", {"editorID": "Gold001"}),
            ("Idles.list", {"ref": "0x1234", "filter": "wave"}),
            ("Idles.play", {"ref": "0x1234", "idle": "IdleWave"}),
            ("Idles.stop", {"ref": "0x1234"}),
            ("Camera.pose", {}),
            ("Camera.mode", {"mode": "vanity"}),
            ("Camera.free", {"on": True, "pos": [1, 2, 3], "yawDeg": 90, "pitchDeg": 10}),
            ("Camera.free", {"on": False}),
            ("Camera.autoVanity", {"allow": False}),
            ("Map.getWaypoint", {}),
            ("Map.setWaypoint", {"x": 100, "y": 200}),
            ("Map.clearWaypoint", {}),
            ("Map.markers", {"contains": "riv", "visible": True}),
        ])


class Tools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.mcp = FakeMcp()

        async def native(method, **params):
            params = {k: v for k, v in params.items() if v is not None}
            self.calls.append((method, params))
            return {"ok": True}

        introspect.register(self.mcp, native)

    async def test_every_tool_is_registered(self):
        self.assertEqual(sorted(self.mcp.tools), sorted([
            "ui_find", "papyrus_classes", "papyrus_functions", "papyrus_call", "forms_find", "forms_info",
            "idles_list", "idles_play", "idles_stop", "camera_pose", "camera_mode", "camera_free",
            "camera_auto_vanity", "map_waypoint", "map_set_waypoint", "map_clear_waypoint", "map_markers"]))

    async def test_papyrus_call_builds_an_alias_target(self):
        await self.mcp.tools["papyrus_call"]("ForceRefTo", args=["0x14"], quest="MQ101", alias_name="Hero")
        await self.mcp.tools["papyrus_call"]("GetPlayer", class_name="Game")
        self.assertEqual(self.calls, [
            ("Papyrus.call", {"function": "ForceRefTo", "args": ["0x14"], "alias": {"quest": "MQ101", "name": "Hero"}}),
            ("Papyrus.call", {"function": "GetPlayer", "class": "Game", "args": []}),
        ])

    async def test_tools_name_the_methods_they_wrap(self):
        await self.mcp.tools["camera_free"](True, pos=[0, 0, 0], yaw_deg=0.0)
        await self.mcp.tools["map_set_waypoint"](1.0, 2.0)
        await self.mcp.tools["forms_find"](type="Idle", plugin="Skyrim.esm")
        self.assertEqual([c[0] for c in self.calls], ["Camera.free", "Map.setWaypoint", "Forms.find"])
        self.assertEqual(self.calls[2][1], {"type": "Idle", "plugin": "Skyrim.esm"})


if __name__ == "__main__":
    unittest.main()
