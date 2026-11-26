import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from skyrim_devtools import livetest
from skyrim_devtools.use.manager import GameError

REQUEST = """
id: {id}
branch: b
sha: abc
touches: {touches}
setup: {setup}
needs_fresh_game: {fresh}
cases:
  - name: ok
    script: {script}
  - name: bad
    script: {script}
    expect: nothing
"""


class FakeSession:
    async def capture(self, path):
        Path(path).write_bytes(b"png")


class FakeManager:
    def __init__(self):
        self.alive = False
        self.starts = 0
        self.stops = 0
        self.events = []
        self.event_total = 0
        self.calls = []
        self.session = FakeSession()
        self.fail_with = None

    def events_since(self, cursor):
        first = self.event_total - len(self.events)
        return self.events[max(cursor, first) - first:], 0

    async def start(self, headless=True, save=None):
        self.alive = True
        self.starts += 1

    async def stop(self):
        self.alive = False
        self.stops += 1

    async def call(self, method, timeout=None, **params):
        self.calls.append(method)
        if self.fail_with and method == "Session.loadSave":
            raise GameError(self.fail_with)
        if method == "Events.snapshot":
            return {"playable": True}
        if method == "Perf.frameStats":
            return {"frame": 1, "count": 600, "fps": 60, "p50Ms": 16, "p99Ms": 20, "maxMs": 30}
        if method == "Perf.hitches":
            return {"hitches": [], "slowCalls": {}}
        return {}


def write(directory, name, text):
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class LiveTest(unittest.TestCase):
    def setUp(self):
        livetest.START_SETTLE = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir, "tests/live/ok.py", "async def run(ctx):\n    ctx.mark('hi')\n    return {'pass': True, 'details': {'n': 1}}\n")
        write(self.dir, "tests/live/no.py", "async def run(ctx):\n    return {'pass': False, 'details': {'n': 2}}\n")
        write(self.dir, "tests/live/standalone.py", "import json\nprint('noise')\nprint(json.dumps({'pass': True, 'details': {'x': 3}}))\n")
        write(self.dir, "tests/live/boom.py", "async def run(ctx):\n    raise RuntimeError('bad ref')\n")
        write(self.dir, "tests/live/slow.py", "import asyncio\nasync def run(ctx):\n    await asyncio.sleep(5)\n")

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, id, script="tests/live/ok.py", touches="[]", setup="a", fresh="false"):
        return livetest.load_request(write(self.dir / "q", f"{id}.yaml",
                                           REQUEST.format(id=id, touches=touches, setup=setup, fresh=fresh, script=script)))

    def run_batch(self, requests, manager=None, **kwargs):
        manager = manager or FakeManager()
        kwargs.setdefault("perf_seconds", 0.01)
        batch = livetest.Batch(manager, self.dir / "q" / "results", self.dir, **kwargs)
        asyncio.run(batch.run(requests))
        return manager, batch

    def test_pass_and_fail_statuses_and_report(self):
        requests = [self.req("r1"), self.req("r2", script="tests/live/no.py")]
        manager, batch = self.run_batch(requests)
        self.assertEqual([o["status"] for o in batch.outcomes["r1"]], ["pass", "pass"])
        self.assertEqual([o["status"] for o in batch.outcomes["r2"]], ["fail", "fail"])
        paths = batch.write_reports(requests)
        text = (self.dir / "q/results/r2.md").read_text()
        self.assertIn("Result: 0/2 pass", text)
        self.assertIn('"n": 2', text)
        self.assertEqual(len(paths), 2)
        self.assertEqual(livetest.summary_line(requests[0], batch.outcomes["r1"]), "r1: 2 pass")

    def test_setup_loaded_once_per_group_and_perf_once(self):
        requests = [self.req("a1", setup="x"), self.req("b1", setup="y"), self.req("a2", setup="x")]
        manager, batch = self.run_batch(requests)
        self.assertEqual(manager.calls.count("Session.loadSave"), 2)
        self.assertEqual(len(batch.perf), 1)
        self.assertEqual(batch.perf[0]["fps"], 60)
        self.assertEqual([r.id for r in batch.plan(requests)], ["a1", "a2", "b1"])

    def test_perf_repeats_after_interval(self):
        manager, batch = self.run_batch([self.req("r1")], perf_interval=0)
        self.assertEqual(len(batch.perf), 2)

    def test_fresh_requests_run_last_with_restart(self):
        requests = [self.req("f", fresh="true"), self.req("n")]
        self.assertEqual([r.id for r in livetest.Batch(FakeManager(), self.dir, self.dir).plan(requests)], ["n", "f"])
        manager, _ = self.run_batch(requests)
        self.assertEqual((manager.starts, manager.stops), (2, 1))

    def test_standalone_script_json(self):
        manager, batch = self.run_batch([self.req("s", script="tests/live/standalone.py")])
        self.assertEqual(batch.outcomes["s"][0]["details"], {"x": 3})
        self.assertEqual(batch.outcomes["s"][0]["status"], "pass")

    def test_command_line_script_gets_config_and_own_game(self):
        write(self.dir, "tests/live/cli.py", "import json, sys\nprint(json.dumps({'pass': '--config' in sys.argv, 'argv': sys.argv[1:]}))\n")
        path = write(self.dir / "q", "cli.yaml", "id: cli\ncases:\n  - name: c\n    script: tests/live/cli.py --phases a,b --config <mac.json>\n  - name: d\n    script: tests/live/cli.py\n")
        request = livetest.load_request(path)
        manager = FakeManager()
        manager.alive = True
        batch = livetest.Batch(manager, self.dir / "q/results", self.dir, config_path="/x/mac.json", perf_seconds=0.01)
        asyncio.run(batch.run([request]))
        first, second = batch.outcomes["cli"]
        self.assertEqual(first["details"]["argv"], ["--phases", "a,b", "--config", "/x/mac.json"])
        self.assertEqual(second["status"], "pass")
        self.assertEqual(manager.stops, 1)
        self.assertEqual(livetest.split_command("a.py x", "/c"), ("a.py", ["x", "--config", "/c"]))

    def test_module_scripts_stay_session_cases_when_a_config_is_given(self):
        # the appended --config must not count as the script's own argument
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "m.py").write_text("async def run(ctx):\n    return {}\n")
            self.assertTrue(livetest.uses_session({"script": "m.py"}, root, "/c"))
            self.assertFalse(livetest.uses_session({"script": "m.py --x 1"}, root, "/c"))

    def test_standalone_scripts_do_not_start_the_shared_game_between_perf_readings(self):
        requests = [self.req("s1", script="tests/live/standalone.py"), self.req("s2", script="tests/live/standalone.py")]
        manager, batch = self.run_batch(requests, perf_interval=3600)
        self.assertEqual(manager.starts, 1)
        self.assertEqual(len(batch.perf), 1)

    def test_last_json_reads_indented_blocks_and_one_liners(self):
        self.assertEqual(livetest.last_json('log\n{\n "pass": true,\n "details": {\n  "a": 1\n }\n}\n'),
                         {"pass": True, "details": {"a": 1}})
        self.assertEqual(livetest.last_json('{"a": 1}\nnoise\n{"b": 2}'), {"b": 2})
        self.assertIsNone(livetest.last_json("nothing here"))

    def test_host_start_failure_is_retried(self):
        write(self.dir, "tests/live/flaky.py", "import json, pathlib, sys\nmark = pathlib.Path(__file__).with_suffix('.mark')\nif not mark.exists():\n    mark.write_text('x')\n    print('RuntimeError: Windows host start failed')\n    sys.exit(1)\nprint(json.dumps({'pass': True, 'details': {}}))\n")
        livetest.START_RETRY_WAIT = 0.01
        manager, batch = self.run_batch([self.req("fl", script="tests/live/flaky.py")])
        self.assertEqual(batch.outcomes["fl"][0]["status"], "pass")

    def test_script_error_and_timeout_are_errors(self):
        manager, batch = self.run_batch([self.req("e", script="tests/live/boom.py")])
        self.assertEqual(batch.outcomes["e"][0]["status"], "error")
        self.assertIn("bad ref", batch.outcomes["e"][0]["error"])
        req = self.req("t", script="tests/live/slow.py")
        batch = livetest.Batch(FakeManager(), self.dir / "q/results", self.dir, perf_seconds=0.01)
        asyncio.run(batch.run([req], case_timeout=0.2))
        self.assertIn("timed out", batch.outcomes["t"][0]["error"])

    def test_setup_failure_marks_cases_without_running_them(self):
        manager = FakeManager()
        manager.fail_with = "save missing"
        manager, batch = self.run_batch([self.req("r1")], manager=manager)
        self.assertEqual([o["status"] for o in batch.outcomes["r1"]], ["error", "error"])
        self.assertIn("setup a failed", batch.notes["r1"][0])

    def test_crash_restarts_game_and_continues(self):
        write(self.dir, "tests/live/die.py", "from skyrim_devtools.use.manager import GameError\nasync def run(ctx):\n    raise GameError('the game is gone (host phase crashed)')\n")
        requests = [self.req("c", script="tests/live/die.py")]
        manager, batch = self.run_batch(requests)
        self.assertEqual(batch.outcomes["c"][0]["status"], "crash")
        self.assertEqual(manager.starts, 2)
        self.assertEqual(len(batch.outcomes["c"]), 2)

    def test_manual_case_takes_screenshot(self):
        path = write(self.dir / "q", "m.yaml", "id: m\ncases:\n  - name: look\n    steps: do it\n    judge: visual\n")
        request = livetest.load_request(path)
        manager, batch = self.run_batch([request])
        self.assertEqual(batch.outcomes["m"][0]["status"], "manual")
        self.assertTrue((self.dir / "q/results/artifacts/m-look-state.png").exists())

    def test_overlap_note_on_failure(self):
        requests = [self.req("p", script="tests/live/no.py", touches="[native/a.cpp]"), self.req("q", touches="[native/a.cpp]")]
        manager, batch = self.run_batch(requests)
        batch.write_reports(requests)
        self.assertIn("ambiguous", (self.dir / "q/results/p.md").read_text())
        self.assertNotIn("ambiguous", (self.dir / "q/results/q.md").read_text())

    def test_queue_loading_skips_broken_and_orders_by_age(self):
        import os
        first = self.req("old")
        os.utime(first.path, (1, 1))
        self.req("new")
        write(self.dir / "q", "junk.yaml", "just: text\n")
        requests, broken = livetest.load_queue(self.dir / "q")
        self.assertEqual([r.id for r in requests], ["old", "new"])
        self.assertEqual(len(broken), 1)

    def test_wait_event_matches_after_cursor(self):
        manager = FakeManager()
        ctx = livetest.Context(manager, self.dir, "x")
        manager.events = [{"type": "cellChange", "n": 1}]
        manager.event_total = 1

        async def go():
            event = await ctx.wait_event("cellChange", 0.3)
            again = await ctx.wait_event("cellChange", 0.3)
            return event, again

        event, again = asyncio.run(go())
        self.assertEqual(event["n"], 1)
        self.assertIsNone(again)


if __name__ == "__main__":
    unittest.main()
