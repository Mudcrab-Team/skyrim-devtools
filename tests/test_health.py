import asyncio
import json
import unittest

from skyrim_devtools.smoke import WindowsHost
from skyrim_devtools.use.health import HealthWatch, evaluate, growth_per_min, parse_health
import test_use
from test_use import Harness, run

RAW = {"running": True, "threads": 300, "handles": 5000, "privateBytes": 3 * 1024 ** 3, "workingSetBytes": 2 * 1024 ** 3,
       "cpuSeconds": 812.3, "cpuCores": 1.4, "responding": True, "uptimeSeconds": 600}


class ParseTests(unittest.TestCase):
    def test_parse_converts_bytes_and_keeps_counts(self):
        health = parse_health(RAW)
        self.assertEqual(health["threads"], 300)
        self.assertEqual(health["privateMB"], 3072.0)
        self.assertEqual(health["workingSetMB"], 2048.0)
        self.assertEqual(health["uptimeS"], 600)
        self.assertTrue(health["responding"])

    def test_not_running_or_garbage_is_none(self):
        self.assertIsNone(parse_health({"running": False}))
        self.assertIsNone(parse_health({"phase": "running"}))
        self.assertIsNone(parse_health(None))
        self.assertIsNone(parse_health({"running": True, "threads": "many"}))


class ThresholdTests(unittest.TestCase):
    def codes(self, health, samples=(), config=None):
        return [w["code"] for w in evaluate(parse_health(dict(RAW, **health)), list(samples), config)]

    def test_quiet_below_every_limit(self):
        self.assertEqual(self.codes({}), [])

    def test_thread_count_threshold_is_configurable(self):
        self.assertEqual(self.codes({"threads": 1500}), ["threadCount"])
        self.assertEqual(self.codes({"threads": 1499}), [])
        self.assertEqual(self.codes({"threads": 400}, config={"healthThreadWarn": 400}), ["threadCount"])

    def test_not_responding_is_an_error(self):
        warnings = evaluate(parse_health(dict(RAW, responding=False)), [])
        self.assertEqual((warnings[0]["code"], warnings[0]["severity"]), ("notResponding", "error"))

    def test_growth_needs_a_span_and_a_rate(self):
        fast = [(0, 135), (30, 195)]
        self.assertIsNone(growth_per_min(fast, 600, 60))
        self.assertEqual(self.codes({}, fast), [])
        steady = [(0, 135), (60, 255), (120, 375)]
        self.assertAlmostEqual(growth_per_min(steady, 600, 60), 120.0)
        self.assertEqual(self.codes({"threads": 375}, steady), ["threadGrowth"])
        slow = [(0, 300), (120, 310)]
        self.assertEqual(self.codes({}, slow), [])

    def test_growth_window_ignores_old_samples(self):
        samples = [(0, 100), (500, 4000), (3000, 4010), (3100, 4020)]
        self.assertLess(growth_per_min(samples, 600, 60), 10)

    def test_long_session_is_informational(self):
        warnings = evaluate(parse_health(dict(RAW, uptimeSeconds=1900)), [])
        self.assertEqual([(w["code"], w["severity"]) for w in warnings], [("longSession", "info")])


class WatchTests(unittest.TestCase):
    def test_watch_accumulates_samples_and_warns_on_growth(self):
        now = [0.0]
        watch = HealthWatch(clock=lambda: now[0])
        for step in range(4):
            now[0] = step * 30.0
            health = watch.record(dict(RAW, threads=200 + step * 90))
        self.assertIn("threadGrowth", [w["code"] for w in health["warnings"]])
        self.assertEqual(watch.latest["threads"], 470)
        self.assertIsNone(watch.record({"running": False}))
        self.assertEqual(watch.warnings, [])


class StatusTests(unittest.TestCase):
    def test_game_status_reports_health_and_warns_over_the_limit(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.host.health = dict(RAW, threads=1800)
            status = json.loads(await h.text("game_status"))
            self.assertEqual(status["health"]["threads"], 1800)
            self.assertEqual(status["healthWarnings"][0]["code"], "threadCount")
            self.assertIn("1800 threads", status["warning"])
            h.host.health = dict(RAW)
            quiet = json.loads(await h.text("game_status"))
            self.assertNotIn("healthWarnings", quiet)
            h.host.health = RuntimeError("down")
            failed = json.loads(await h.text("game_status"))
            self.assertIn("error", failed["health"])
            await h.text("game_stop")
        run(scenario())

    def test_health_threshold_comes_from_the_config(self):
        async def scenario():
            h = Harness()
            h.manager.config["healthThreadWarn"] = 100
            h.manager.health_watch = type(h.manager.health_watch)(h.manager.config)
            await h.text("game_start")
            h.host.health = dict(RAW, threads=150)
            status = json.loads(await h.text("game_status"))
            self.assertEqual(status["healthWarnings"][0]["code"], "threadCount")
            await h.text("game_stop")
        run(scenario())

    def test_start_reports_what_it_cleared(self):
        async def scenario():
            h = Harness()
            h.host.stale = ["task-stopped", "lease-removed"]
            started = json.loads(await h.text("game_start"))
            self.assertEqual(started["staleCleared"], ["task-stopped", "lease-removed"])
            await h.text("game_stop")
        run(scenario())


class HostStopTests(unittest.TestCase):
    def host(self, states):
        host = WindowsHost({"sshDestination": "worker"})
        calls = []

        async def command(action, session_id="", mode="", grace=None, force=False):
            calls.append((action, force))
            if action == "status":
                return states.pop(0) if len(states) > 1 else states[0]
            return {"cleared": []}

        host.command = command
        return host, calls

    def test_stop_resets_the_host_after_the_session_ends(self):
        async def scenario():
            host, calls = self.host([{"phase": "stopped"}])
            result = await host.stop("a" * 32)
            self.assertEqual(result["phase"], "stopped")
            self.assertEqual(calls[-1], ("reset", False))
        run(scenario())

    def test_stop_kills_leftovers_when_the_session_never_ends(self):
        async def scenario():
            host, calls = self.host([{"phase": "running"}])
            with self.assertRaises(TimeoutError):
                await host.stop("a" * 32, grace=1)
            self.assertEqual(calls[-1], ("reset", True))
        run(scenario())


if __name__ == "__main__":
    unittest.main()
