import asyncio
import json
import unittest

from skyrim_devtools.client import ConnectionLost, Session
from skyrim_devtools.logs import PapyrusErrors, classify_outcome, heartbeat


class FakePlugin:
    """Answers the logging and liveness methods from canned state."""

    def __init__(self):
        self.inbox = asyncio.Queue()
        self.lines = []
        self.dropped = 0
        self.main_stalled = 0
        self.ui_stalled = 0
        self.silent = False
        self.subscribed = set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.inbox.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, message):
        self.inbox.put_nowait(json.dumps(message))

    def emit(self, kind, data):
        self.push({"jsonrpc": "2.0", "method": "event", "params": {"type": kind, "seq": 0, "epoch": 0, "data": data}})

    def add(self, severity, text):
        self.lines.append({"seq": len(self.lines) + 1, "ms": 0, "severity": severity, "text": text})

    async def send(self, text):
        request = json.loads(text)
        method, params = request["method"], request["params"]
        if self.silent:
            return
        if method == "Papyrus.log":
            rank = {"info": 0, "warning": 1, "error": 2, "fatal": 3}
            lines = [line for line in self.lines
                     if line["seq"] > params.get("since", 0) and rank[line["severity"]] >= rank[params.get("level", "info")]]
            lines = lines[len(lines) - params.get("limit", 500):] if params.get("limit", 500) < len(lines) else lines
            result = {"lines": lines, "lastSeq": len(self.lines), "dropped": self.dropped}
        elif method == "Health.heartbeat":
            result = {"uptimeMs": 1000, "main": {"stalledMs": self.main_stalled}, "ui": {"stalledMs": self.ui_stalled}}
        elif method == "Events.subscribe":
            self.subscribed.update(params["types"])
            result = {"subscribed": sorted(self.subscribed)}
        else:
            self.push({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": method}})
            return
        self.push({"jsonrpc": "2.0", "id": request["id"], "result": result})

    async def close(self):
        self.inbox.put_nowait(None)


class LogsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = FakePlugin()
        self.session = Session(self.plugin, "0" * 32)

    async def asyncTearDown(self):
        await self.plugin.close()

    async def test_papyrus_watch_reports_only_new_errors(self):
        self.plugin.add("error", "old failure")
        async with self.session.papyrus_watch() as papyrus:
            self.plugin.add("info", "noise")
            self.plugin.add("warning", "slow")
            self.plugin.add("error", "Cannot call Foo() on a None object")
        self.assertEqual([line["text"] for line in papyrus.errors], ["Cannot call Foo() on a None object"])
        self.assertEqual(len(papyrus.warnings), 1)
        with self.assertRaises(PapyrusErrors) as caught:
            papyrus.assert_clean()
        self.assertIn("Cannot call Foo", str(caught.exception))

    async def test_papyrus_watch_strict_and_ignore(self):
        async with self.session.papyrus_watch(strict=True, ignore=[r"known noise"]) as papyrus:
            self.plugin.add("error", "known noise from a vanilla script")
        self.assertEqual(papyrus.errors, [])
        with self.assertRaises(PapyrusErrors):
            async with self.session.papyrus_watch(strict=True):
                self.plugin.add("fatal", "stack overflow")

    async def test_strict_watch_does_not_mask_a_scenario_failure(self):
        with self.assertRaises(ZeroDivisionError):
            async with self.session.papyrus_watch(strict=True):
                self.plugin.add("error", "boom")
                1 / 0

    async def test_watch_filters_by_type_and_iterates(self):
        async with self.session.watch("death", "combat") as events:
            self.assertEqual(self.plugin.subscribed, {"death", "combat"})
            self.plugin.emit("hit", {"x": 1})
            self.plugin.emit("death", {"victim": "a"})
            self.plugin.emit("combat", {"state": "combat"})
            first = await events.next(timeout=1)
            second = await events.next(timeout=1)
        self.assertEqual([first["type"], second["type"]], ["death", "combat"])
        with self.assertRaises(StopAsyncIteration):
            await events.__anext__()

    async def test_watch_counts_overflow(self):
        async with self.session.watch("hit", limit=2) as events:
            for index in range(5):
                self.plugin.emit("hit", {"i": index})
            await asyncio.sleep(0.05)
        self.assertEqual(events.dropped, 3)

    async def test_heartbeat_states(self):
        self.assertEqual((await heartbeat(self.session)).status, "ok")
        self.plugin.main_stalled = 8000
        reading = await heartbeat(self.session)
        self.assertEqual(reading.status, "busy")
        self.assertIn("main", reading.detail)
        self.plugin.ui_stalled = 45000
        reading = await heartbeat(self.session)
        self.assertEqual(reading.status, "hang")
        self.assertIn("UI", reading.detail)
        self.assertFalse(reading.alive)

    async def test_heartbeat_unresponsive_and_lost(self):
        self.plugin.silent = True
        self.assertEqual((await heartbeat(self.session, timeout=0.1)).status, "unresponsive")
        self.plugin.silent = False
        await self.plugin.close()
        await asyncio.sleep(0.05)
        with self.assertRaises(ConnectionLost):
            await self.session.call("Session.ping")
        self.assertEqual((await heartbeat(self.session)).status, "lost")

    async def test_monitor_latches_a_hang(self):
        self.plugin.main_stalled = 40000
        monitor = self.session.monitor(interval=0.01, hang_after=30)
        try:
            async with asyncio.timeout(2):
                await monitor.hang_detected.wait()
        finally:
            monitor.stop()
        self.assertTrue(monitor.hung)
        self.assertEqual(monitor.status, "hang")


class OutcomeTest(unittest.TestCase):
    def test_classification(self):
        cases = [
            ({"phase": "stopped", "exitCode": 0}, "clean"),
            ({"phase": "crashed", "reason": "nonzero-exit", "exitCode": -1073741819}, "crash"),
            ({"phase": "forced", "reason": "requested", "hung": True}, "hang"),
            ({"phase": "forced", "reason": "watchdog", "hung": False}, "forced"),
            ({"phase": "exited", "reason": "unrequested-exit", "exitCode": 0}, "unexpected-exit"),
            ({"phase": "failed", "reason": "preflight"}, "startup-failure"),
            ({"phase": "running", "hangSuspectedSince": "x"}, "hang"),
            ({"phase": "running"}, "running"),
        ]
        for state, kind in cases:
            self.assertEqual(classify_outcome(state).kind, kind, state)

    def test_crash_summary_names_the_exception_and_module(self):
        evidence = {"crashEvents": [{"faultingModule": "Example.dll", "exceptionCode": "c0000005"}]}
        outcome = classify_outcome({"phase": "crashed", "reason": "nonzero-exit", "exitCode": -1073741819}, evidence)
        self.assertFalse(outcome.clean)
        self.assertIn("0xc0000005", outcome.summary)
        self.assertIn("Example.dll", outcome.summary)


if __name__ == "__main__":
    unittest.main()
