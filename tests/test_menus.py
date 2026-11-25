import asyncio
import json
import unittest

from skyrim_devtools.client import Session


class Recorder:
    """Records each request and answers with an empty result."""

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


class MenuHelpers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = Recorder()
        self.session = Session(self.socket, "0" * 32)

    async def asyncTearDown(self):
        await self.socket.inbox.put(None)

    async def test_unset_optionals_are_not_sent(self):
        await self.session.select_item("InventoryMenu", text="Iron Sword")
        await self.session.message_box_select(index=1)
        await self.session.ui_dump("Main Menu")
        await self.session.dialogue_state()
        self.assertEqual(self.socket.requests, [
            ("UI.selectItem", {"menu": "InventoryMenu", "text": "Iron Sword"}),
            ("UI.messageBoxSelect", {"index": 1}),
            ("UI.dump", {"menu": "Main Menu"}),
            ("Dialogue.state", {}),
        ])

    async def test_zero_values_are_kept(self):
        await self.session.select_item("ContainerMenu", index=0, action="take", count=0)
        self.assertEqual(self.socket.requests[-1][1],
                         {"menu": "ContainerMenu", "index": 0, "action": "take", "count": 0})

    async def test_invoke_sends_empty_args(self):
        await self.session.ui_invoke("Main Menu", "_root.Foo.bar")
        self.assertEqual(self.socket.requests[-1][1], {"menu": "Main Menu", "path": "_root.Foo.bar", "args": []})


if __name__ == "__main__":
    unittest.main()
