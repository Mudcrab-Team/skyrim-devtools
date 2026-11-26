import asyncio
import json
import unittest
from unittest import mock

import test_use
from skyrim_devtools.use import server
from test_use import Harness, run

METHODS = test_use.METHODS + ["Move.walk", "Move.stop", "Nav.goto", "Nav.status", "Nav.cancel"]
STUCK_DATA = {"reason": "stuck", "failures": 4, "stuckS": 14.2, "pos": [10.0, 20.0, 30.0], "facingDeg": 90.0,
              "blockers": {"refs": []}, "screenshotHint": "Call screenshot now"}


class StuckTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(test_use, "METHODS", METHODS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_walk_reports_stuck_with_details(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].errors["Move.walk"] = {"code": -32011, "message": "stuck", "data": STUCK_DATA}
            result = json.loads(await h.text("walk", direction="forward", duration_ms=30000, stuck={"windowS": 2}))
            self.assertTrue(result["stuck"])
            self.assertEqual(result["error"], "stuck")
            self.assertEqual(result["pos"], [10.0, 20.0, 30.0])
            self.assertIn("blockers", result)
            sent = [c for c in h.sockets[0].calls if c[0] == "Move.walk"][0][1]
            self.assertEqual(sent["stuck"], {"windowS": 2})
            await h.text("game_stop")
        run(scenario())

    def test_other_walk_errors_still_raise(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].errors["Move.walk"] = {"code": -32009, "message": "busy"}
            with self.assertRaises(Exception):
                await h.text("walk", duration_ms=1000)
            await h.text("game_stop")
        run(scenario())

    def test_nav_wait_surfaces_stuck(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Nav.status"] = {"active": False, "last": {
                "state": "failed", "reason": "stuck (noProgress)", "stuck": {**STUCK_DATA, "reason": "noProgress"}}}
            result = json.loads(await h.text("nav_wait", timeout_s=5, poll_s=0.25))
            self.assertTrue(result["done"])
            self.assertEqual(result["error"], "stuck")
            self.assertEqual(result["stuck"]["reason"], "noProgress")
            await h.text("game_stop")
        run(scenario())

    def test_nav_wait_leaves_other_failures_alone(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Nav.status"] = {"active": False, "last": {"state": "failed", "reason": "Timed out", "stuck": None}}
            result = json.loads(await h.text("nav_wait", timeout_s=5, poll_s=0.25))
            self.assertNotIn("error", result)
            await h.text("game_stop")
        run(scenario())

    def test_hard_cap_stops_a_player_that_does_not_move(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Player.state"] = {"pos": {"x": 5.0, "y": 5.0, "z": 0.0}}
            with mock.patch.object(server, "MOVE_HARD_CAP_S", 0.3), mock.patch.object(server, "MOVE_CAP_POLL_S", 0.05):
                async with server.MovementCap(h.manager) as cap:
                    await asyncio.sleep(0.8)
            self.assertEqual(cap.tripped["reason"], "hardCap")
            self.assertTrue(cap.tripped["stuck"])
            stopped = [c[0] for c in h.sockets[0].calls]
            self.assertIn("Move.stop", stopped)
            self.assertIn("Nav.cancel", stopped)
            await h.text("game_stop")
        run(scenario())

    def test_hard_cap_ignores_a_moving_player(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            counter = {"x": 0.0}

            original = h.sockets[0].send

            async def moving(text):
                counter["x"] += 100.0
                h.sockets[0].results["Player.state"] = {"pos": [counter["x"], 0.0, 0.0]}
                await original(text)

            h.sockets[0].send = moving
            with mock.patch.object(server, "MOVE_HARD_CAP_S", 0.3), mock.patch.object(server, "MOVE_CAP_POLL_S", 0.05):
                async with server.MovementCap(h.manager) as cap:
                    await asyncio.sleep(0.8)
            self.assertIsNone(cap.tripped)
            await h.text("game_stop")
        run(scenario())

    def test_stuck_events_reach_the_timeline(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            for kind in ("moveStuck", "moveUnstuck", "menuOpenClose"):
                h.sockets[0].push({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": kind, "seq": 1, "epoch": 0, "data": {"source": "walk", "reason": "stuck"}}})
            await asyncio.sleep(0.1)
            await h.text("game_stop")
            lines = [json.loads(line) for line in h.recorder.path.read_text().splitlines()]
            events = [line for line in lines if line["t"] == "event"]
            self.assertEqual([e["type"] for e in events], ["moveStuck", "moveUnstuck"])
            self.assertEqual(events[0]["data"]["source"], "walk")
        run(scenario())


if __name__ == "__main__":
    unittest.main()
