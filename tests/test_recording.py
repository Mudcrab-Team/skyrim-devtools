import asyncio
import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recording_fake import FakeGame  # noqa: E402

from skyrim_devtools import bench, recording  # noqa: E402
from skyrim_devtools.use import recording_tools  # noqa: E402


def no_sleep(_):
    async def done():
        return None

    return done()


class DocTests(unittest.TestCase):
    def setUp(self):
        self.doc = FakeGame().make_doc("walk", seconds=10)

    def test_valid_document_passes_and_summarises(self):
        recording.validate(self.doc)
        info = recording.summary(self.doc)
        self.assertEqual(info["name"], "walk")
        self.assertEqual(info["durationS"], 10.0)
        self.assertEqual(info["tier"], "anchored")
        self.assertGreater(info["distance"], 500)

    def test_rejects_bad_documents(self):
        for mutate, text in (
            (lambda d: d.update(format="other"), "not a"),
            (lambda d: d.update(version=2), "unsupported version"),
            (lambda d: d.update(samples=d["samples"][:1]), "fewer than two"),
            (lambda d: d["samples"][3].__setitem__(0, 0), "back in time"),
            (lambda d: d["samples"][2].__setitem__(1, "x"), "six numbers"),
            (lambda d: d.update(events=[{"type": "cell"}]), "needs t and type"),
            (lambda d: d["meta"].update(coupling={"tier": "weird"}), "coupling tier"),
        ):
            doc = copy.deepcopy(self.doc)
            mutate(doc)
            with self.assertRaisesRegex(recording.RecordingError, text):
                recording.validate(doc)

    def test_load_round_trip_and_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(self.doc))
            self.assertEqual(recording.load(path)["meta"]["name"], "walk")
            with self.assertRaises(recording.RecordingError):
                recording.load(Path(tmp) / "missing.json")

    def test_coupling_tiers_by_entry_age(self):
        def tier(age, kind="save", meta=None):
            doc = {"entry": {"kind": kind, "ageMs": age}, "meta": meta or {}}
            return recording.coupling_tier(doc)

        self.assertEqual(tier(0), "anchored")
        self.assertEqual(tier(10_000), "anchored")
        self.assertEqual(tier(10_001), "cell")
        self.assertEqual(tier(60_000), "cell")
        self.assertEqual(tier(60_001), "worldspace")
        self.assertEqual(tier(5, kind="unknown"), "worldspace")
        self.assertEqual(recording.coupling_tier({"entry": {"kind": "save"}}), "worldspace")

    def test_coupling_override_per_recording(self):
        self.assertEqual(recording.coupling_tier({"entry": {"kind": "save", "ageMs": 5}, "meta": {"coupling": {"tier": "cell"}}}), "cell")
        doc = {"entry": {"kind": "coc", "ageMs": 20_000}, "meta": {"coupling": {"anchorMs": 30_000}}}
        self.assertEqual(recording.coupling_tier(doc), "anchored")
        doc = {"entry": {"kind": "coc", "ageMs": 20_000}, "meta": {"coupling": {"cellMs": 5_000}}}
        self.assertEqual(recording.coupling_tier(doc), "worldspace")


class DivergenceTests(unittest.TestCase):
    def rows(self, offset=0.0, step=100):
        return [[t, t * 0.1 + offset, 0.0, 0.0] for t in range(0, 5001, step)]

    def test_identical_paths_have_zero_divergence(self):
        d = recording.divergence(self.rows(), self.rows(step=250))
        self.assertEqual(d["medianUnits"], 0.0)
        self.assertEqual(d["maxUnits"], 0.0)

    def test_constant_offset_is_measured(self):
        d = recording.divergence(self.rows(offset=30.0), self.rows())
        self.assertAlmostEqual(d["medianUnits"], 30.0, places=1)
        self.assertAlmostEqual(d["p95Units"], 30.0, places=1)

    def test_teleport_pairs_are_skipped(self):
        reference = [[0, 0, 0, 0], [1000, 5000, 0, 0], [2000, 5010, 0, 0]]
        trace = [[500, 9999, 9999, 0], [1500, 5005, 0, 0]]
        d = recording.divergence(trace, reference)
        self.assertEqual(d["count"], 1)
        self.assertAlmostEqual(d["medianUnits"], 0.0, places=1)

    def test_empty_trace(self):
        self.assertEqual(recording.divergence([], self.rows())["count"], 0)

    def test_pose_clamps_outside_the_recording(self):
        rows = self.rows()
        self.assertEqual(recording.pose_at(rows, -10), (0.0, 0.0, 0.0))
        self.assertEqual(recording.pose_at(rows, 99999), (500.0, 0.0, 0.0))

    def test_percentile(self):
        self.assertEqual(recording.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertIsNone(recording.percentile([], 50))
        self.assertEqual(recording.percentile([7], 95), 7.0)


class ReplayToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_waits_and_measures_divergence(self):
        game = FakeGame(noise=2.0)
        game.docs["walk"] = game.make_doc("walk", seconds=20)
        result = await recording_tools.replay_and_compare(game.call, "walk", sleep=no_sleep, restoreScene=True)
        self.assertTrue(result["replay"]["finished"])
        self.assertLess(result["divergenceVsRecording"]["medianUnits"], 5.0)
        self.assertGreater(result["divergenceVsRecording"]["count"], 100)

    async def test_failed_replay_reports_error_without_divergence(self):
        game = FakeGame(fail_replays={1})
        game.docs["walk"] = game.make_doc("walk", seconds=5)
        result = await recording_tools.replay_and_compare(game.call, "walk", sleep=no_sleep)
        self.assertFalse(result["replay"]["finished"])
        self.assertIn("restoreTimeout", result["replay"]["error"])
        self.assertNotIn("divergenceVsRecording", result)

    async def test_wait_false_returns_the_start(self):
        game = FakeGame()
        game.docs["walk"] = game.make_doc("walk", seconds=5)
        started = await recording_tools.replay_and_compare(game.call, "walk", wait=False)
        self.assertEqual(started["id"], 1)

    async def test_timeout_stops_the_replay(self):
        game = FakeGame()
        game.m_Recording_status = lambda: {"replay": {"id": 1, "phase": "restore"}, "lastReplay": None}
        ticks = iter(range(0, 1000, 100))
        with self.assertRaises(TimeoutError):
            await recording_tools.wait_replay(game.call, 1, 50, sleep=no_sleep, clock=lambda: next(ticks))
        self.assertIn("Recording.stopReplay", [c[0] for c in game.calls])


class StatsTests(unittest.TestCase):
    def test_spread(self):
        s = bench.spread([1, 2, 3, 4])
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["mean"], 2.5)
        self.assertAlmostEqual(s["sd"], math.sqrt(5 / 3), places=3)
        self.assertEqual(bench.spread([None])["n"], 0)
        self.assertIsNone(bench.spread([5])["sd"])

    def test_compare_flags_a_clear_difference(self):
        a, b = bench.spread([16.0, 16.1, 15.9]), bench.spread([17.0, 17.1, 16.9])
        d = bench.compare(a, b)
        self.assertTrue(d["significant"])
        self.assertEqual(d["verdict"], "B worse")
        self.assertAlmostEqual(d["delta"], 1.0, places=2)

    def test_compare_calls_noise_noise(self):
        a, b = bench.spread([16.0, 17.0, 15.0]), bench.spread([16.2, 17.1, 15.1])
        d = bench.compare(a, b)
        self.assertFalse(d["significant"])
        self.assertEqual(d["verdict"], "within noise")

    def test_higher_is_better_flips_the_verdict(self):
        a, b = bench.spread([60.0, 60.1, 59.9]), bench.spread([50.0, 50.1, 49.9])
        self.assertEqual(bench.compare(a, b, higher_is_better=True)["verdict"], "B worse")
        self.assertEqual(bench.compare(b, a, higher_is_better=True)["verdict"], "B better")

    def test_single_run_cannot_judge_noise(self):
        d = bench.compare(bench.spread([16.0]), bench.spread([17.0]))
        self.assertIsNone(d["significant"])
        self.assertIn("at least 2 runs", d["verdict"])

    def test_no_data(self):
        self.assertEqual(bench.compare(bench.spread([]), bench.spread([1.0]))["verdict"], "no data")

    def test_normalize_accepts_a_list(self):
        c = bench.normalize(["tgm"], "A")
        self.assertEqual(c["console"], ["tgm"])
        self.assertEqual(c["label"], "A")


class BenchTests(unittest.IsolatedAsyncioTestCase):
    async def run_bench(self, game, a, b, **kwargs):
        game.docs["walk"] = game.make_doc("walk", seconds=30)
        return await bench.bench_ab(game.call, "walk", a, b, has_method=game.has_method, profile=False, sleep=no_sleep, **kwargs)

    async def test_detects_a_real_difference_and_orders_runs(self):
        game = FakeGame(hour_cost_ms=0.1, frame_noise_ms=0.05)
        report = await self.run_bench(game, {"label": "noon", "console": ["set GameHour to 12"]},
                                      {"label": "midnight", "console": ["set GameHour to 0"]}, runs=3)
        self.assertEqual(report["a"]["goodRuns"], 3)
        self.assertEqual(report["b"]["goodRuns"], 3)
        p50 = report["delta"]["p50Ms"]
        self.assertAlmostEqual(p50["delta"], -1.2, delta=0.2)
        self.assertTrue(p50["significant"])
        self.assertEqual(p50["verdict"], "B worse" if p50["delta"] > 0 else "B better")
        self.assertIn("p99Ms", report["delta"])
        self.assertIn("hitches", report["delta"])
        replays = [c for c in game.calls if c[0] == "Recording.replay"]
        self.assertEqual([c[1]["beforeRun"][0] for c in replays], ["set GameHour to 12", "set GameHour to 0"] * 3)
        self.assertEqual(game.hitch_ms, 50.0)

    async def test_identical_configs_are_within_noise(self):
        game = FakeGame(frame_noise_ms=0.3)
        report = await self.run_bench(game, ["set GameHour to 12"], ["set GameHour to 12"], runs=4)
        self.assertEqual(report["a"]["label"], "A")
        self.assertFalse(report["delta"]["p50Ms"]["significant"])
        self.assertIsNotNone(report["delta"]["p50Ms"]["noiseSd"])

    async def test_blocks_order(self):
        game = FakeGame()
        report = await self.run_bench(game, ["set GameHour to 1"], ["set GameHour to 2"], runs=2, order="blocks")
        labels = [r["config"] for side in ("a", "b") for r in report[side]["runs"]]
        self.assertEqual(labels, ["A", "A", "B", "B"])
        replays = [c[1]["beforeRun"][0] for c in game.calls if c[0] == "Recording.replay"]
        self.assertEqual(replays, ["set GameHour to 1"] * 2 + ["set GameHour to 2"] * 2)

    async def test_failed_run_is_excluded_and_reported(self):
        game = FakeGame(fail_replays={3})
        report = await self.run_bench(game, ["set GameHour to 12"], ["set GameHour to 0"], runs=3)
        failed = [r for side in ("a", "b") for r in report[side]["runs"] if "metrics" not in r]
        self.assertEqual(len(failed), 1)
        self.assertEqual(report["a"]["goodRuns"] + report["b"]["goodRuns"], 5)
        self.assertTrue(any("failed" in w for w in report["warnings"]))

    async def test_rpc_toggles_and_cleanup(self):
        game = FakeGame()
        a = {"rpc": [{"method": "Console.execute", "params": {"command": "set GameHour to 3"}}],
             "cleanup": {"console": ["set GameHour to 12"]}}
        report = await self.run_bench(game, a, ["set GameHour to 4"], runs=1)
        self.assertEqual(report["a"]["goodRuns"], 1)
        self.assertEqual(game.calls[-1][0], "Console.execute")
        self.assertEqual(game.calls[-1][1]["command"], "set GameHour to 12")

    async def test_argument_checks(self):
        game = FakeGame()
        with self.assertRaises(bench.BenchError):
            await bench.bench_ab(game.call, "walk", [], [], runs=0, duration_s=1)
        with self.assertRaises(bench.BenchError):
            await bench.bench_ab(game.call, "walk", [], [], order="random", duration_s=1)

    async def test_text_report_lists_metrics_and_warnings(self):
        game = FakeGame()
        report = await self.run_bench(game, ["set GameHour to 12"], ["set GameHour to 0"], runs=2)
        text = bench.render_text(report)
        self.assertIn("p50Ms", text)
        self.assertIn("p99Ms", text)
        self.assertIn("no GPU pass times", text)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_register_and_bench_runs_through_them(self):
        from mcp.server.fastmcp import FastMCP

        game = FakeGame()
        game.docs["walk"] = game.make_doc("walk", seconds=10)

        async def native(method, **params):
            return await game.call(method, **{k: v for k, v in params.items() if v is not None})

        class Manager:
            methods = game.methods
            has_method = staticmethod(game.has_method)

        mcp = FastMCP("t")
        recording_tools.register(mcp, native, Manager())
        names = {tool.name for tool in await mcp.list_tools()}
        self.assertTrue({"recording_start", "recording_stop", "recording_list", "replay", "replay_stop", "bench_ab"} <= names)
        out = await mcp.call_tool("bench_ab", {"recording": "walk", "a": ["set GameHour to 12"], "b": ["set GameHour to 0"],
                                               "runs": 2, "profile": False})
        text = out[0][0].text if isinstance(out, tuple) else out[0].text
        report = json.loads(text)
        self.assertIn("p99Ms", report["delta"])
        self.assertIn("text", report)


class LiveScriptDryRun(unittest.IsolatedAsyncioTestCase):
    async def test_live_script_passes_against_the_fake_game(self):
        import importlib.util

        path = Path(__file__).resolve().parent / "live" / "record_replay.py"
        spec = importlib.util.spec_from_file_location("live_record_replay", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = await module.dry_run()
        self.assertTrue(result["pass"], result["details"]["problems"])
        self.assertEqual(result["details"]["bench"]["goodRuns"], [3, 3])


class MetricsTests(unittest.TestCase):
    def test_frame_metrics_with_gpu(self):
        gpu = {"gpuMsP50": 9.5, "passes": [{"pass": "shadows", "msPerFrame": 2.0}]}
        m = bench.frame_metrics({"count": 10, "p50Ms": 16.0, "hitches": None}, {"hitches": [1, 2]}, gpu)
        self.assertEqual(m["hitches"], 2)
        self.assertEqual(m["gpuPasses"], {"shadows": 2.0})


if __name__ == "__main__":
    unittest.main()
