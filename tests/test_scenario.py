import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from skyrim_devtools import video
from skyrim_devtools.use import scenario as sc
from test_use import Harness


def run(coro):
    return asyncio.run(coro)


class FakeGame:
    """Records calls and answers like the plugin for the methods a scenario uses."""

    def __init__(self, stage=0, running=False, targets=None, fail=()):
        self.calls = []
        self.stage = stage
        self.running = running
        self.fail = set(fail)
        self.targets = targets if targets is not None else [
            {"text": "Enter the barrow", "target": {"refID": "0001"}, "tracking": {"refID": "0002", "name": "Door"}, "distance": 100}]

    async def __call__(self, method, timeout=None, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise RuntimeError(f"{method} failed")
        if method == "Quest.get":
            return {"stage": self.stage, "running": self.running}
        if method == "Scenario.setStage":
            before, self.stage = self.stage, params["stage"]
            return {"before": before, "after": self.stage}
        if method == "Scenario.character":
            return {"steps": [{"step": "item", "ok": True}, {"step": "perk", "ok": False, "reason": "No such perk", "perk": "Nope"}]}
        if method == "Quest.targets":
            return {"targets": self.targets}
        if method == "Events.snapshot":
            return {"playable": True, "menus": []}
        if method == "Player.teleport":
            return {"queued": True}
        if method == "Player.state":
            return {"cell": "X", "pos": {"x": 1, "y": 2, "z": 3}, "level": 10}
        return {}

    def methods(self):
        return [c[0] for c in self.calls]


class PlanTests(unittest.TestCase):
    def test_kit_items_are_replaced_by_explicit_entries(self):
        items = sc.item_list({"kit": "medium", "items": [{"item": "Gold", "count": 9}, "Torch"]})
        gold = [i for i in items if i["item"] == "Gold"]
        self.assertEqual(gold, [{"item": "Gold", "count": 9}])
        self.assertEqual(items[-1], {"item": "Torch"})
        self.assertTrue(any(i.get("equip") for i in items))

    def test_unknown_field_and_kit_are_rejected(self):
        with self.assertRaises(sc.ScenarioError):
            sc.validate({"kitt": "light"})
        with self.assertRaises(sc.ScenarioError):
            sc.validate({"kit": "huge"})
        with self.assertRaises(sc.ScenarioError):
            sc.validate({"stage": 10})
        with self.assertRaises(sc.ScenarioError):
            sc.validate({"position": {"ref": "1", "cell": "A"}})
        with self.assertRaises(sc.ScenarioError):
            sc.validate({"level": 200})
        sc.validate({"position": {"cell": "WhiterunBanneredMare", "x": 1, "y": 2, "z": 3}})

    def test_plan_order(self):
        spec = {"quest": "Q", "stage": 20, "kit": "light", "time": 9, "position": {"questTarget": "Q"}}
        self.assertEqual(sc.plan(spec), ["character", "time", "quest", "position", "verify"])

    def test_stage_path_explicit_and_guessed(self):
        self.assertEqual(sc.stage_path({"stage": 40, "completeStagesBefore": True, "stagePath": [10, 25, 30, 50]}, 10), ([25, 30], False))
        self.assertEqual(sc.stage_path({"stage": 40, "completeStagesBefore": True}, 0), ([10, 20, 30], True))
        self.assertEqual(sc.stage_path({"stage": 40}, 0), ([], False))

    def test_risk_warnings(self):
        skip = sc.risk_warnings({"quest": "Q", "stage": 40}, 0, False)
        self.assertIn("skips the stages", skip[0])
        self.assertEqual(sc.risk_warnings({"quest": "Q", "stage": 10}, 0, True), [])
        self.assertTrue(any("rewind" in w for w in sc.risk_warnings({"quest": "Q", "stage": 10}, 30, True)))
        guess = sc.risk_warnings({"quest": "Q", "stage": 40, "completeStagesBefore": True}, 0, False)
        self.assertTrue(any("guesses" in w for w in guess))

    def test_load_json_and_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.json").write_text(json.dumps({"kit": "light", "level": 5}))
            self.assertEqual(sc.load_scenario(Path(tmp, "a.json"))["level"], 5)
            Path(tmp, "b.yaml").write_text("kit: heavy\nskills:\n  OneHanded: 60\nposition:\n  place: Whiterun\n")
            loaded = sc.load_scenario(Path(tmp, "b.yaml"))
            self.assertEqual(loaded["skills"], {"OneHanded": 60})
            self.assertEqual(loaded["position"], {"place": "Whiterun"})

    def test_example_scenarios_are_valid(self):
        for path in sorted((Path(__file__).resolve().parent.parent / "scenarios").glob("*.yaml")):
            spec = sc.load_scenario(path)
            self.assertTrue(sc.plan(spec), path.name)


class RunTests(unittest.TestCase):
    def test_runs_steps_in_order_and_reports_failures(self):
        game = FakeGame(stage=0)
        spec = {"quest": "Q", "stage": 30, "completeStagesBefore": True, "stagePath": [10, 20], "kit": "light",
                "level": 10, "position": {"questTarget": "Q"}, "time": 9}
        report = run(sc.run_scenario(game, spec, settle=0))
        order = game.methods()
        self.assertLess(order.index("Scenario.character"), order.index("Console.execute") if "Console.execute" in order else 99)
        stages = [p["stage"] for m, p in game.calls if m == "Scenario.setStage"]
        self.assertEqual(stages, [10, 20, 30])
        self.assertIn(("Player.teleport", {"refID": "0002"}), game.calls)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failures"][0]["step"], "perk")
        self.assertEqual(report["questTargets"][0]["text"], "Enter the barrow")
        self.assertEqual(report["player"]["level"], 10)

    def test_failed_step_does_not_stop_the_rest(self):
        game = FakeGame(fail={"Scenario.character"})
        report = run(sc.run_scenario(game, {"kit": "light", "position": {"ref": "0003"}}, settle=0))
        self.assertEqual(report["failures"][0]["step"], "character")
        self.assertIn(("Player.teleport", {"refID": "0003"}), game.calls)

    def test_skipped_stages_warn(self):
        report = run(sc.run_scenario(FakeGame(stage=0), {"quest": "Q", "stage": 40}, settle=0))
        self.assertTrue(any("skips the stages" in w for w in report["warnings"]))

    def test_no_marker_warns(self):
        report = run(sc.run_scenario(FakeGame(targets=[]), {"quest": "Q", "stage": 10}, settle=0))
        self.assertTrue(any("no marker" in w for w in report["warnings"]))

    def test_position_without_target_fails_cleanly(self):
        report = run(sc.run_scenario(FakeGame(targets=[]), {"position": {"questTarget": "Q"}}, settle=0))
        self.assertEqual(report["failures"][0]["step"], "position")
        self.assertTrue(any("not moved" in w for w in report["warnings"]))


class ToolTests(unittest.TestCase):
    def test_tools_call_plugin_and_are_timelined(self):
        async def scenario():
            h = Harness()
            await h.text("game_start", save="Fixture")
            for method in ("Game.speed", "Game.setSpeed", "Game.fastTravel", "Game.waitHours", "Scenario.character"):
                h.manager.methods[method] = {}
            socket = h.sockets[0]
            socket.results["Game.waitHours"] = {"waitedHours": 3.0, "hourBefore": 9.0, "hour": 12.0}
            socket.results["Game.setSpeed"] = {"walkPercent": 300}
            socket.results["Game.fastTravel"] = {"to": "Whiterun", "hours": 3.5}
            waited = json.loads(await h.text("wait", hours=3))
            self.assertEqual(waited["hour"], 12.0)
            speed = json.loads(await h.text("game_speed", walk=300, timescale=5))
            self.assertEqual(speed["walkPercent"], 300)
            self.assertIn(("Game.setSpeed", {"walk": 300, "timescale": 5}), socket.calls)
            await h.text("game_speed", reset=True)
            self.assertIn(("Game.setSpeed", {"reset": True}), socket.calls)
            travelled = json.loads(await h.text("fast_travel", to="Whiterun", force=True))
            self.assertTrue(travelled["playable"])
            self.assertIn(("Game.fastTravel", {"to": "Whiterun", "force": True, "skipTime": True}), socket.calls)
            report = json.loads(await h.text("scenario_setup", kit="light", level=3))
            self.assertIn("applied", report)
            lines = [json.loads(line) for line in h.recorder.path.read_text().splitlines()]
            names = [r["name"] for r in lines if r["t"] == "tool"]
            for name in ("wait", "game_speed", "fast_travel", "scenario_setup"):
                self.assertIn(name, names)
            wait_line = next(r for r in lines if r.get("name") == "wait")
            self.assertEqual(wait_line["args"], {"hours": 3.0})
            self.assertIn("waitedHours", wait_line["summary"])
            await h.text("game_stop")
        run(scenario())

    def test_new_tools_are_sped_up_in_videos(self):
        for name in ("wait", "fast_travel", "scenario_setup"):
            self.assertTrue(video.is_boring(name))

    def test_bad_scenario_is_a_tool_error(self):
        async def scenario():
            h = Harness()
            await h.text("game_start", save="Fixture")
            with self.assertRaises(Exception):
                await h.text("scenario_setup", kit="enormous")
            await h.text("game_stop")
        run(scenario())


if __name__ == "__main__":
    unittest.main()


class LiveScriptTests(unittest.TestCase):
    def test_live_script_runs_against_the_fake_plugin(self):
        import contextlib
        import io
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent / "live"))
        import scenario_speed

        async def scenario():
            h = Harness()
            real_start = h.manager.start

            async def start(headless=True, save=None):
                result = await real_start(headless=headless, save=save)
                for method in ("Game.speed", "Game.setSpeed", "Game.fastTravel", "Game.waitHours", "Game.mapMarkers",
                               "Scenario.character", "Scenario.setStage", "Quest.get", "Quest.find", "Quest.targets",
                               "Game.time", "Nav.places", "Player.teleport", "Session.reload", "Move.walk"):
                    h.manager.methods[method] = {}
                h.sockets[0].results.update({
                    "Game.time": {"daysPassed": 1.0}, "Quest.get": {"stage": 10}, "Player.state": {"level": 3, "pos": [1, 2, 3]},
                    "Game.fastTravel": {"to": "Whiterun"}, "Game.waitHours": {"waitedHours": 30}, "Game.mapMarkers": {"markers": []},
                    "Scenario.character": {"steps": []}, "Scenario.setStage": {"after": 10}, "Quest.targets": {"targets": []},
                    "Move.walk": {"distance": 1000.0}, "Game.setSpeed": {"speedMult": 100}})
                return result

            h.manager.start = start

            config_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            config_file.write("{}")
            config_file.close()

            class Options:
                config = config_file.name
                cases = "speed,lovely,travel"
                headless = True
                dry_run = False

            scenario_speed.GameManager = lambda config: h.manager
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                await scenario_speed.main(Options)
            return json.loads(buffer.getvalue())

        out = run(scenario())
        self.assertIn("speed", out["details"])
        self.assertIn("lovely", out["details"])
        self.assertIn("travel", out["details"])
