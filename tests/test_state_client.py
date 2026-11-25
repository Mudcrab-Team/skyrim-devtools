import asyncio
import json
import unittest

from skyrim_devtools.client import Session


class RecordingSocket:
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
        result = {"ref": None} if request["method"] == "World.crosshair" else {"value": 1.5}
        self.inbox.put_nowait(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))


class StateClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_helpers_send_only_the_given_parameters(self):
        socket = RecordingSocket()
        session = Session(socket, "0" * 32)
        session.epoch = 3
        await session.inventory(filter="weapon")
        await session.nearby(types=["door"], limit=5)
        await session.quest(editor_id="MQ101")
        await session.form(form_id="0x14")
        await session.teleport(x=1, y=2, z=3)
        await session.add_item("0xF", 10)
        self.assertIsNone(await session.crosshair())
        self.assertEqual(await session.global_value(editor_id="TimeScale"), 1.5)
        self.assertEqual(socket.requests, [
            ("Player.inventory", {"filter": "weapon", "limit": 100}),
            ("World.nearby", {"radius": 1000, "types": ["door"], "limit": 5}),
            ("Quest.get", {"editorID": "MQ101"}),
            ("Form.get", {"formID": "0x14"}),
            ("Player.teleport", {"x": 1, "y": 2, "z": 3, "loadEpoch": 3}),
            ("Player.addItem", {"formID": "0xF", "count": 10, "loadEpoch": 3}),
            ("World.crosshair", {}),
            ("Global.get", {"editorID": "TimeScale"}),
        ])
        session._reader.cancel()


if __name__ == "__main__":
    unittest.main()
