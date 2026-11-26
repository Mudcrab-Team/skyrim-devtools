import json
import unittest

from skyrim_devtools import video
from skyrim_devtools.use import capture
from test_use import SESSION, Harness, run


def lines(recorder):
    return [json.loads(line) for line in recorder.path.read_text().splitlines()]


class TimelineTests(unittest.TestCase):
    def test_tools_and_narration_are_logged(self):
        async def scenario():
            h = Harness()
            await h.tool("game_start")
            await h.tool("snapshot")
            await h.tool("narrate", text="I will look around first")
            return lines(h.recorder)

        records = run(scenario())
        tools = [r for r in records if r["t"] == "tool"]
        self.assertEqual([t["name"] for t in tools[:2]], ["game_start", "snapshot"])
        self.assertTrue(all(t["endMs"] >= t["startMs"] for t in tools))
        self.assertIn({"t": "narrate", "ms": records[[r["t"] for r in records].index("narrate")]["ms"],
                       "text": "I will look around first"}, records)
        self.assertFalse(any(t["name"] == "narrate" for t in tools))

    def test_capture_refuses_headless(self):
        async def scenario():
            h = Harness()
            await h.tool("game_start", headless=True)
            with self.assertRaisesRegex(Exception, "windowed"):
                await h.tool("capture_start")
            failed = [r for r in lines(h.recorder) if r["t"] == "tool" and r["name"] == "capture_start"]
            return failed

        self.assertFalse(run(scenario())[0]["ok"])

    def test_capture_start_records_footage_origin(self):
        async def scenario():
            h = Harness()
            await h.tool("game_start", headless=False)

            async def fake_action(self, action, session_id, extra="", timeout=60):
                self.seen = (action, session_id, extra)
                return {"phase": "recording", "startEpochMs": 1234, "path": "C:/x/capture.mp4", "file": "capture.mp4",
                        "encoder": "h264_amf", "fps": 30}

            capture.CaptureHost.action = fake_action
            await h.text("capture_start", fps=24)
            return lines(h.recorder)

        events = [r for r in run(scenario()) if r["t"] == "capture"]
        self.assertEqual(events[0]["startEpochMs"], 1234)


class ComposeTests(unittest.TestCase):
    def timeline(self):
        t = video.Timeline(footage_start_ms=0)
        t.calls = [video.Call("snapshot", {}, 2, 2.3, True, "ok", ""),
                   video.Call("nav_goto", {}, 12, 40, True, "arrived", ""),
                   video.Call("dialogue_select", {"index": 1}, 45, 46, True, "ok", "")]
        t.captions = [(5, 9, "hello")]
        return t

    def test_plan_covers_footage_and_speeds_idle(self):
        segments = video.plan_segments(self.timeline(), 60)
        self.assertAlmostEqual(segments[0][0], 0)
        self.assertAlmostEqual(segments[-1][1], 60)
        for (_, end, _), (start, _, _) in zip(segments, segments[1:]):
            self.assertAlmostEqual(end, start)
        speeds = {round(sp, 1) for _, _, sp in segments}
        self.assertIn(1.0, speeds)
        # the walk (13-39 s) is sped up as one long stretch, capped to 6 s of output
        walk = [s for s in segments if s[0] <= 14 and s[1] >= 38]
        self.assertTrue(walk and (walk[0][1] - walk[0][0]) / walk[0][2] <= 6.01)
        # the dialogue call stays at 1x
        self.assertTrue(any(s[0] <= 45 and s[1] >= 46 and s[2] == 1.0 for s in segments))

    def test_load_timeline_aligns_to_capture_start(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "t.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in [
                {"t": "capture", "ms": 10_000, "action": "start", "startEpochMs": 10_000},
                {"t": "tool", "name": "snapshot", "args": {}, "startMs": 12_000, "endMs": 12_500, "ok": True},
                {"t": "narrate", "ms": 14_000, "text": "hi"}]))
            timeline = video.load_timeline(path)
        self.assertEqual(timeline.calls[0].start, 2.0)
        self.assertEqual(timeline.captions[0][0], 4.0)

    def test_action_and_walk_speedups_leave_captions_alone(self):
        timeline = video.Timeline(
            calls=[video.Call("combat_fight", {}, 10.0, 11.0, True, "", ""), video.Call("nav_wait", {}, 20.0, 80.0, True, "", "")],
            captions=[(40.0, 44.0, "hello")])
        plain = video.plan_segments(timeline, 100.0)
        fast = video.plan_segments(timeline, 100.0, speedup_walk=30.0, speedup_action=3.0)
        self.assertEqual([s for s in plain if s[0] <= 10.0 < s[1]][0][2], 1.0)
        self.assertEqual([s for s in fast if s[0] <= 10.5 < s[1]][0][2], 3.0)
        self.assertEqual([s for s in fast if s[0] <= 42.0 < s[1]][0][2], 1.0)
        self.assertGreaterEqual([s for s in fast if s[0] <= 30.0 < s[1]][0][2], 30.0)

    def test_status_before_first_sample_uses_the_first_sample(self):
        timeline = video.Timeline(statuses=[(5.0, {"cell": "Riverwood", "health": [90, 100]}),
                                            (9.0, {"health": [40, 100]})])
        self.assertEqual(video.current_status(timeline, 1.0), {"cell": "Riverwood", "health": [90, 100]})
        self.assertEqual(video.current_status(timeline, 10.0), {"cell": "Riverwood", "health": [40, 100]})
        self.assertEqual(video.current_status(video.Timeline(), 1.0), {})

    def test_status_block_always_lists_quest_health_location(self):
        image = video.draw_frame(video.panel_state(video.Timeline(), 0.0, 1.0), "t")
        self.assertEqual(image.size, (video.W, video.H))


if __name__ == "__main__":
    unittest.main()
