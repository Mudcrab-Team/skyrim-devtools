import json
import tempfile
import unittest
from pathlib import Path

from skyrim_devtools.use.alerts import AlertBoard, digest
from skyrim_devtools.video import load_timeline
from test_use import Harness, run


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def make_board():
    clock = Clock()
    records = []
    return AlertBoard(write=records.append, clock=clock), clock, records


class BoardTests(unittest.TestCase):
    def test_raise_and_clear_write_timeline_records(self):
        board, clock, records = make_board()
        self.assertTrue(board.raise_("lowFps", "warn", "pause", "fps 12"))
        self.assertFalse(board.raise_("lowFps", "warn", "pause", "fps 11"))
        self.assertEqual(board.active["lowFps"]["detail"], "fps 11")
        clock.now += 42
        self.assertEqual(board.compact(), [{"kind": "lowFps", "severity": "warn", "since": "42s", "hint": "pause"}])
        self.assertTrue(board.clear("lowFps"))
        self.assertFalse(board.clear("lowFps"))
        self.assertEqual([r["action"] for r in records], ["raised", "cleared"])
        self.assertEqual(records[1]["durationMs"], 42000)
        self.assertEqual([h["action"] for h in board.history], ["raised", "cleared"])

    def test_native_events(self):
        board, _, _ = make_board()
        board.on_event({"type": "alertRaised", "data": {"kind": "deathLoop", "severity": "error", "hint": "kit up",
                                                         "detail": "died 2 times", "sinceMs": 5}})
        self.assertEqual(board.active["deathLoop"]["sinceMs"], 5)
        board.on_event({"type": "alertCleared", "data": {"kind": "deathLoop", "why": "no recent deaths"}})
        self.assertEqual(board.active, {})

    def test_move_stuck_events_become_an_alert(self):
        board, _, _ = make_board()
        board.on_event({"type": "moveStuck", "data": {"reason": "door loop"}})
        self.assertEqual(board.active["moveStuck"]["detail"], "door loop")
        board.on_event({"type": "moveUnstuck", "data": {}})
        self.assertNotIn("moveStuck", board.active)

    def test_move_stuck_clears_on_arrival_new_goal_and_cell_change(self):
        for ending in (lambda b: b.on_event({"type": "navArrived", "data": {}}),
                       lambda b: b.call_started("nav_goto"),
                       lambda b: b.call_started("center_on_cell"),
                       lambda b: b.on_event({"type": "cellChange", "data": {"entered": True}})):
            board, _, records = make_board()
            board.on_event({"type": "moveStuck", "data": {"reason": "wall"}})
            ending(board)
            self.assertNotIn("moveStuck", board.active)
            self.assertEqual(records[-1]["action"], "cleared")

    def test_other_calls_and_leaving_a_cell_keep_the_stuck_alert(self):
        board, _, _ = make_board()
        board.on_event({"type": "moveStuck", "data": {}})
        board.call_started("screenshot")
        board.on_event({"type": "cellChange", "data": {"entered": False}})
        self.assertIn("moveStuck", board.active)

    def test_loading_a_save_clears_every_alert(self):
        board, _, _ = make_board()
        board.on_event({"type": "moveStuck", "data": {}})
        board.raise_("lowFps", "warn", "pause", source="native")
        board.on_event({"type": "loadGame", "data": {"success": True}})
        self.assertEqual(board.active, {})

    def test_action_loop_needs_same_call_same_result(self):
        board, clock, _ = make_board()
        for _ in range(2):
            board.observe_call("activate", {"ref_id": "0x1"}, '{"ok":true}')
            clock.now += 10
        self.assertNotIn("actionLoop", board.active)
        board.observe_call("activate", {"ref_id": "0x1"}, '{"ok":true}')
        self.assertIn("actionLoop", board.active)
        self.assertIn("3 times", board.active["actionLoop"]["detail"])
        # a changed result is progress and ends the alert
        board.observe_call("activate", {"ref_id": "0x1"}, '{"ok":false}')
        self.assertNotIn("actionLoop", board.active)

    def test_action_loop_window_and_exempt_tools(self):
        board, clock, _ = make_board()
        for _ in range(5):
            board.observe_call("wait_for", {"timeout_s": 1}, "x")
        self.assertEqual(board.active, {})
        for _ in range(3):
            board.observe_call("dialogue_select", {"index": 0}, "same")
            clock.now += 70
        self.assertNotIn("actionLoop", board.active)

    def test_action_loop_ignores_changing_frame_counters_and_expires(self):
        board, clock, _ = make_board()
        for frame in range(3):
            board.observe_call("snapshot", {}, json.dumps({"playable": True, "seq": frame}))
        self.assertIn("actionLoop", board.active)
        clock.now += 31
        board.expire()
        self.assertNotIn("actionLoop", board.active)

    def test_blind_camera_from_screenshot_stats(self):
        board, _, _ = make_board()
        board.observe_screenshot({"meanLuma": 90.0, "dominantShare": 0.1})
        self.assertEqual(board.active, {})
        board.observe_screenshot({"meanLuma": 3.0, "dominantShare": 0.99})
        self.assertIn("blindCamera", board.active)
        board.observe_screenshot({"meanLuma": 80.0, "dominantShare": 0.2})
        self.assertEqual(board.active, {})

    def test_attach_shapes(self):
        board, _, _ = make_board()
        self.assertEqual(board.attach('{"a":1}'), '{"a":1}')
        board.raise_("crime", "warn", "pay it", "bounty 40")
        self.assertEqual(json.loads(board.attach('{"a":1}'))["alerts"][0]["kind"], "crime")
        self.assertTrue(board.attach("plain text").startswith("plain text\nalerts: ["))
        self.assertEqual(board.attach(["img", "meta"])[-1][:8], "alerts: ")
        self.assertEqual(len(board.attach("[1,2]").splitlines()), 2)

    def test_dismiss_and_configure(self):
        board, _, _ = make_board()
        board.raise_("questStalled", "warn", "x", "y")
        board.dismiss("questStalled", 60)
        self.assertFalse(board.raise_("questStalled", "warn", "x", "y"))
        rest = board.configure({"actionLoopCount": 5, "questStalledMin": 3}, disable=["crime"])
        self.assertEqual(rest, {"questStalledMin": 3})
        self.assertEqual(board.config["actionLoopCount"], 5)
        self.assertFalse(board.raise_("crime", "warn", "x", "y"))

    def test_sync_adopts_and_drops_native_alerts(self):
        board, _, _ = make_board()
        board.raise_("mine", "info", "h")
        board.raise_("gone", "warn", "h", source="native")
        board.sync({"active": [{"kind": "lowFps", "severity": "warn", "hint": "h", "detail": "d", "sinceMs": 9}]})
        self.assertEqual(sorted(board.active), ["lowFps", "mine"])

    def test_digest_distinguishes_images(self):
        class Img:
            def __init__(self, data):
                self.data = data

        self.assertEqual(digest([Img(b"a"), "m"]), digest([Img(b"a"), "m"]))
        self.assertNotEqual(digest([Img(b"a")]), digest([Img(b"b")]))


class ServerTests(unittest.TestCase):
    def test_every_tool_reply_carries_alerts_and_agent_alerts_reports_them(self):
        async def scenario():
            h = Harness()
            await h.text("game_start", save="Fixture")
            h.manager.methods["Alerts.state"] = {"name": "Alerts.state"}
            h.sockets[0].results["Alerts.state"] = {"active": [{"kind": "menuLeftOpen", "severity": "warn", "hint": "close it", "detail": "InventoryMenu", "sinceMs": 1}],
                                                    "cost": {"ticks": 3}, "thresholds": {"pollMs": 500},
                                                    "disabled": []}
            self.assertNotIn("alerts", json.loads(await h.text("snapshot")))
            h.manager._on_event({"type": "alertRaised", "seq": 1, "epoch": 0, "data": {
                "kind": "menuLeftOpen", "severity": "warn", "hint": "close it", "detail": "InventoryMenu", "sinceMs": 1}})
            reply = json.loads(await h.text("snapshot"))
            self.assertEqual(reply["alerts"][0]["kind"], "menuLeftOpen")
            drained = json.loads(await h.text("events_drain"))
            self.assertFalse([e for e in drained["events"] if e["type"] == "alertRaised"])
            console = await h.text("console", command="GetAV Health", target="player")
            self.assertIn("\nalerts: [", console)
            full = json.loads(await h.text("agent_alerts", history=True))
            self.assertEqual(full["active"][0]["kind"], "menuLeftOpen")
            self.assertEqual(full["history"][0]["action"], "raised")
            self.assertNotIn("alerts", full)
            self.assertEqual(full["cost"], {"ticks": 3})
            await h.text("game_stop")
            self.assertFalse(h.manager.alive)
        run(scenario())

    def test_repeated_call_raises_action_loop_and_is_on_the_timeline(self):
        async def scenario():
            h = Harness()
            await h.text("game_start", save="Fixture")
            for _ in range(3):
                reply = await h.text("snapshot")
            self.assertEqual(json.loads(reply)["alerts"][0]["kind"], "actionLoop")
            lines = [json.loads(line) for line in h.recorder.path.read_text().splitlines()]
            alerts = [r for r in lines if r["t"] == "alert"]
            self.assertEqual(alerts[0]["kind"], "actionLoop")
            self.assertEqual(alerts[0]["action"], "raised")
            await h.text("game_stop")
        run(scenario())

    def test_video_timeline_collects_alert_intervals(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.jsonl"
            rows = [{"t": "capture", "action": "start", "startEpochMs": 1000, "ms": 1000},
                    {"t": "alert", "ms": 3000, "action": "raised", "kind": "lowFps", "severity": "warn"},
                    {"t": "alert", "ms": 6000, "action": "cleared", "kind": "lowFps"}]
            path.write_text("\n".join(json.dumps(r) for r in rows))
            timeline = load_timeline(path)
            self.assertEqual(timeline.alerts, [[2.0, 5.0, "lowFps", "warn"]])


if __name__ == "__main__":
    unittest.main()


class SamplerFlagTests(unittest.TestCase):
    def test_flags_that_are_off_are_written_false(self):
        import asyncio
        from skyrim_devtools.use.timeline import Recorder

        class Manager:
            def has_method(self, name):
                return name == "Player.state"

            async def call(self, method, **params):
                return {"cell": "Riverwood", "inCombat": False, "sneaking": False, "dead": False}

        with tempfile.TemporaryDirectory() as folder:
            sample = asyncio.run(Recorder(folder).sample(Manager()))
        self.assertIs(sample["inCombat"], False)
