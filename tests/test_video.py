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

    def test_state_flags_come_from_the_latest_sample_only(self):
        timeline = video.Timeline(statuses=[(1.0, {"cell": "Mine", "inCombat": True, "sneaking": True}),
                                            (9.0, {"cell": "Shop", "health": [90, 100]})])
        self.assertTrue(video.current_status(timeline, 5.0)["inCombat"])
        later = video.current_status(timeline, 20.0)
        self.assertEqual(later, {"cell": "Shop", "health": [90, 100]})

    def test_status_block_always_lists_quest_health_location(self):
        image = video.draw_frame(video.panel_state(video.Timeline(), 0.0, 1.0), "t")
        self.assertEqual(image.size, (video.W, video.H))


if __name__ == "__main__":
    unittest.main()


class HighlightTests(unittest.TestCase):
    def test_parse_interval_accepts_seconds_and_clock(self):
        self.assertEqual(video.parse_interval("90-1:45"), (90.0, 105.0))
        self.assertEqual(video.parse_interval("1:00:00-1:00:30.5"), (3600.0, 3630.5))
        with self.assertRaises(ValueError):
            video.parse_interval("50-40")
        with self.assertRaises(ValueError):
            video.parse_interval("50")

    def test_narration_windows_pad_and_merge(self):
        t = video.Timeline(footage_start_ms=0)
        t.captions = [(10, 15, "a"), (18, 22, "b"), (100, 104, "c")]
        self.assertEqual(video.narration_windows(t, before=2, after=3), [[8, 25], [98, 107]])

    def test_subtract_cuts_holes(self):
        self.assertEqual(video.subtract([[0, 100]], [(10, 20), (50, 60)]), [[0, 10], [20, 50], [60, 100]])
        self.assertEqual(video.subtract([[0, 10]], [(0, 10)]), [])

    def test_restrict_keeps_only_windows_and_their_speeds(self):
        segments = [(0, 10, 1.0), (10, 40, 5.0), (40, 50, 1.0)]
        self.assertEqual(video.restrict(segments, [[5, 15], [45, 60]]),
                         [(5, 10, 1.0), (10, 15, 5.0), (45, 50, 1.0)])

    def test_fit_length_caps_the_speed_up(self):
        segments = [(0, 100, 1.0), (100, 200, 2.0)]
        fitted, factor = video.fit_length(segments, 75)
        self.assertAlmostEqual(factor, 2.0)
        self.assertAlmostEqual(sum((e - s) / sp for s, e, sp in fitted), 75)
        same, factor = video.fit_length(segments, 500)
        self.assertEqual((same, factor), (segments, 1.0))

    def test_jumps_fade_but_continuous_segments_do_not(self):
        graph = video.filter_script([(0, 10, 1.0), (10, 20, 4.0), (50, 60, 1.0)], 0, 0, fade=0.25)
        lines = [line for line in graph.split(";\n") if "trim=" in line]
        self.assertNotIn("fade", lines[0])
        self.assertIn("fade=t=out", lines[1])
        self.assertIn("fade=t=in", lines[2])

    def test_capture_stop_gives_the_footage_length(self):
        import tempfile, pathlib
        records = [{"t": "capture", "action": "start", "startEpochMs": 1000, "ms": 1000},
                   {"t": "narrate", "ms": 5000, "text": "x"},
                   {"t": "capture", "action": "stop", "startEpochMs": 1000, "stopEpochMs": 61000, "ms": 61000}]
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "t.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records))
            self.assertEqual(video.load_timeline(path).footage_seconds, 60.0)


class PresentationRecordTests(unittest.TestCase):
    def load(self, records):
        import tempfile, pathlib
        folder = tempfile.mkdtemp()
        path = pathlib.Path(folder) / "t.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in
                                  [{"t": "capture", "action": "start", "startEpochMs": 1000, "ms": 1000}] + records))
        return video.load_timeline(path), pathlib.Path(folder)

    def test_slides_insets_metrics_and_banners_are_loaded(self):
        timeline, folder = self.load([
            {"t": "chapter", "ms": 3000, "title": "Profiler"},
            {"t": "slide", "ms": 11000, "image": "flame.png", "durationS": 5},
            {"t": "inset", "ms": 5000, "image": "/abs/map.png", "durationS": 4, "label": "map"},
            {"t": "metrics", "ms": 6000, "rows": [["FPS", "60"]]},
            {"t": "metrics", "ms": 9000, "hide": True},
            {"t": "event", "ms": 7000, "type": "moveStuck", "data": {"reason": "wall"}},
            {"t": "event", "ms": 8000, "type": "moveUnstuck", "data": {}},
            {"t": "alert", "ms": 20000, "action": "raised", "kind": "lowHealth"}])
        self.assertEqual([(t, d, s["kind"]) for t, d, s in timeline.slides], [(2.0, 3.0, "chapter"), (10.0, 5.0, "slide")])
        self.assertEqual(timeline.slides[1][2]["image"], str(folder / "flame.png"))
        self.assertEqual(timeline.insets, [(4.0, 8.0, "/abs/map.png", "map")])
        perf = lambda at: json.loads(video.panel_state(timeline, at, 1.0)[3]).get("perf")
        self.assertIsNone(perf(1))
        self.assertEqual(perf(6)["rows"], [["FPS", "60"]])
        self.assertIsNone(perf(9))
        # the stuck banner stays up at least 2.5 s even when the unstuck event comes sooner
        self.assertEqual(timeline.banners[0], [6.0, 8.5, "STUCK DETECTED: wall"])
        self.assertEqual(timeline.banners[1][2], "ALERT: lowHealth")
        state = video.panel_state(timeline, 7, 1.0)
        self.assertEqual(json.loads(state[3])["banner"], "STUCK DETECTED: wall")

    def test_slides_split_segments_and_land_between_them(self):
        segments = video.split_at([(0, 10, 1.0), (10, 30, 4.0)], [15.0, 40.0])
        self.assertEqual(segments, [(0, 10, 1.0), (10, 15.0, 4.0), (15.0, 30, 4.0)])
        self.assertEqual(video.slide_positions(segments, [15.0, 0.0, 99.0]), [2, 0, 3])

    def test_graph_wires_insets_before_the_split_and_slides_into_the_concat(self):
        graph = video.filter_script([(0, 10, 1.0), (10, 20, 1.0)], 3.5, 4.0, insets=[(2, 6, 900, 50)], slides=[(1, 4.0)])
        self.assertIn("[4:v]format=yuv420p[ins0]", graph)
        self.assertIn("enable='between(t,2.000,6.000)'", graph)
        self.assertIn("[5:v]fps=30", graph)
        concat = graph.split(";\n")[-1]
        self.assertTrue(concat.startswith("[intro][s0][sl0][s1][outro]concat=n=5"), concat)

    def test_frames_with_perf_box_and_banner_draw(self):
        try:
            video.need_pillow()
        except SystemExit:
            self.skipTest("Pillow not installed")
        status = {"perf": {"title": "PROFILER top", "badge": "GPU bound", "rows": [["FPS", "60.0"], ["GPU", "84 W"]],
                           "spark": [16.6, 17.0, 33.0, 16.7]}, "banner": "STUCK DETECTED: wall"}
        image = video.draw_frame(((), None, "hello", json.dumps(status), 12.0), "title")
        self.assertEqual(image.size, (video.W, video.H))

    def test_insets_and_banners_play_at_normal_speed(self):
        timeline, _ = self.load([
            {"t": "tool", "name": "nav_goto", "args": {}, "startMs": 1000, "endMs": 61000, "ok": True},
            {"t": "inset", "ms": 11000, "image": "m.png", "durationS": 5},
            {"t": "event", "ms": 31000, "type": "moveStuck", "data": {}}])
        segments = video.plan_segments(timeline, 60, speedup=10)
        self.assertEqual(video.speed_at(segments, 12.0), 1.0)
        self.assertEqual(video.speed_at(segments, 31.0), 1.0)
        self.assertGreater(video.speed_at(segments, 45.0), 1.0)
