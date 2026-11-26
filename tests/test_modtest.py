import asyncio
import json
import os
import tempfile
import unittest

from skyrim_devtools.use.manager import GameError
from skyrim_devtools.use.modtest import (DialogueExplorer, coverage_markdown, counts_delta, explore_markdown,
                                         pick_next, unreachable)


def topic(text, never=True, info=None):
    return {"index": 0, "text": text, "neverSaid": never, "topic": "T" + text[:3], "info": info or "I" + text[:3]}


class FakeConversation:
    """A small invented NPC: choosing a job answer changes the world, the rumor menu does not."""

    def __init__(self):
        self.stage = 0
        self.saved = None
        self.log = []
        self.menu = None
        self.current = []
        self.open = False

    def topics(self):
        if self.menu == "job":
            return ["Accept", "Refuse"]
        if self.menu == "rumor":
            return ["Tell me more", "Never mind"]
        base = ["Rumors", "Goodbye"]
        return (["Job"] if self.stage == 0 else ["Reward"]) + base

    def shown(self):
        out = []
        for i, text in enumerate(self.topics()):
            out.append({"index": i, "text": text, "neverSaid": True, "topic": "T" + text, "info": "I" + text})
        self.current = out
        return {"status": "topics", "topics": out, "lines": [], "said": [], "stateEvents": []}

    async def start(self):
        self.open, self.menu = True, None
        self.log.append("start")
        step = self.shown()
        step["lines"] = ["Hello."]
        step["said"] = [{"info": "Igreet", "topicText": "Greeting", "questEditorID": "Q", "plugin": "Mod.esp"}]
        return step

    async def select(self, text):
        self.log.append(f"select {text}")
        assert text in [t["text"] for t in self.current], text
        said = [{"info": "I" + text, "topicText": text, "questEditorID": "Q", "plugin": "Mod.esp"}]
        events = []
        if text == "Goodbye":
            self.open = False
            return {"status": "closed", "topics": [], "lines": ["Bye."], "said": said, "stateEvents": []}
        if text == "Job":
            self.menu = "job"
        elif text in ("Accept", "Refuse"):
            self.menu = None
            self.stage = 1 if text == "Accept" else 0
            if text == "Accept":
                events = [{"quest": "Q", "stage": 10}]
        elif text == "Rumors":
            self.menu = "rumor"
        elif text in ("Tell me more", "Never mind"):
            self.menu = None
        step = self.shown()
        step.update(lines=[f"line for {text}"], said=said, stateEvents=events)
        return step

    async def close(self):
        self.open = False
        self.log.append("close")

    async def save(self):
        self.saved = self.stage
        self.log.append("save")

    async def reload(self):
        self.stage = self.saved
        self.log.append("reload")


def run(coro):
    return asyncio.run(coro)


class ExplorerTests(unittest.TestCase):
    def test_exclusive_siblings_are_both_tried_with_one_reload(self):
        fake = FakeConversation()
        report = run(DialogueExplorer(fake, strategy="unseen-first").run())
        choices = [(tuple(b["path"]), b["choice"]) for b in report["branches"]]
        self.assertIn((("Job",), "Accept"), choices)
        self.assertIn((("Job",), "Refuse"), choices)
        self.assertIn("Tell me more", [c for _, c in choices])
        self.assertEqual(report["reloads"], 1)
        accept = [b for b in report["branches"] if b["choice"] == "Accept"][0]
        self.assertTrue(accept["stateChanged"])
        self.assertEqual(fake.stage, 0, "the world is back to the saved state when Refuse was tried")
        self.assertEqual(report["stopReason"], "done")
        self.assertFalse(report["unexplored"])
        self.assertIn("Igreet", {i["info"] for i in report["saidInfos"]})

    def test_breadth_visits_shallow_branches_first(self):
        fake = FakeConversation()
        report = run(DialogueExplorer(fake, strategy="breadth").run())
        depths = [len(b["path"]) for b in report["branches"]]
        self.assertEqual(depths, sorted(depths))
        self.assertEqual({b["choice"] for b in report["branches"]},
                         {"Job", "Accept", "Refuse", "Reward", "Rumors", "Goodbye", "Tell me more", "Never mind"})

    def test_branch_budget_leaves_unexplored(self):
        report = run(DialogueExplorer(FakeConversation(), max_branches=2).run())
        self.assertEqual(len(report["branches"]), 2)
        self.assertEqual(report["stopReason"], "branch budget")
        self.assertTrue(report["unexplored"])

    def test_time_budget(self):
        ticks = iter(range(0, 1000, 100))
        report = run(DialogueExplorer(FakeConversation(), time_budget_s=150, clock=lambda: next(ticks)).run())
        self.assertEqual(report["stopReason"], "time budget")

    def test_rejects_unknown_strategy(self):
        with self.assertRaises(GameError):
            DialogueExplorer(FakeConversation(), strategy="random")

    def test_pick_next_orders(self):
        queue = [{"path": (), "neverSaid": False, "seq": 1}, {"path": (), "neverSaid": True, "seq": 2},
                 {"path": ("a", "b"), "neverSaid": False, "seq": 3}]
        self.assertEqual(pick_next(queue, "unseen-first")["seq"], 2)
        self.assertEqual(pick_next(queue, "breadth")["seq"], 1)
        self.assertEqual(pick_next(queue[:1] + queue[2:], "unseen-first")["seq"], 3)


class ReachabilityTests(unittest.TestCase):
    def test_unreachable_splits_blocked_and_unheard(self):
        topics = {"topics": [
            {"formID": "T1", "text": "Job", "kind": "player", "quest": {"editorID": "Q1", "plugin": "A.esp", "running": True},
             "infos": [{"formID": "I1", "plugin": "A.esp", "passes": True},
                       {"formID": "I2", "plugin": "A.esp", "passes": False, "failing": [58]},
                       {"formID": "I3", "plugin": "A.esp", "passes": True}]},
            {"formID": "T2", "text": "bark", "kind": "combat", "quest": None,
             "infos": [{"formID": "I9", "plugin": "A.esp", "passes": True}]}]}
        result = unreachable(topics, [{"info": "I1"}])
        self.assertEqual([i["formID"] for i in result["passingButNotHeard"]], ["I3"])
        self.assertEqual(result["blockedByConditions"][0]["failing"], [58])
        text = explore_markdown("Someone", {"strategy": "breadth", "branches": [], "elapsedS": 1, "reloads": 0,
                                            "selects": 1, "stopReason": "done", "unexplored": []}, result)
        self.assertIn("Blocked by conditions: 1", text)


REPORT = {
    "durationS": 12.5, "plugins": ["Mod.esp"],
    "stages": {"hit": [{"editorID": "Q1", "formID": "1", "stage": 10, "plugin": "Mod.esp", "atS": 3.0}],
               "defined": 4, "covered": 1, "quests": 2, "missed": []},
    "infos": {"said": [{"quest": "Q1", "topicText": "Hi", "formID": "2", "via": "watcher"}], "defined": 9, "covered": 1, "missed": []},
    "scenes": {"started": [], "defined": 1}, "questsStarted": [],
    "papyrus": {"errors": [{"atS": 4.0, "severity": "error", "text": "Cannot call X on a None object"}], "errorCount": 1,
                "warnings": [], "warningCount": 0, "scriptsLogging": [{"script": "MyScript"}]},
}


class CoverageTests(unittest.TestCase):
    def test_markdown_summary(self):
        text = coverage_markdown(REPORT, {"questStage": 2, "hit": 0})
        self.assertIn("| Quest stages | 1 | 4 in 2 quests |", text)
        self.assertIn("| Dialogue infos | 1 | 9 |", text)
        self.assertIn("1 errors, 0 warnings", text)
        self.assertIn("questStage 2", text)
        self.assertNotIn("hit 0", text)

    def test_counts_delta(self):
        before = {"hit": {"seen": 2, "emitted": 0}}
        after = {"hit": {"seen": 5, "emitted": 0}, "death": {"seen": 1, "emitted": 0}}
        self.assertEqual(counts_delta(before, after), {"hit": 3, "death": 1})


class ToolWiringTests(unittest.TestCase):
    def test_tools_forward_params_and_write_files(self):
        import test_use
        from test_use import Harness

        async def scenario():
            os.environ["SKYRIM_USE_COVERAGE_DIR"] = tempfile.mkdtemp()
            h = Harness()
            await h.text("game_start")
            sock = h.sockets[0]
            sock.results["Coverage.start"] = {"started": True}
            sock.results["Coverage.report"] = json.loads(json.dumps(REPORT))
            sock.results["GameEvents.counts"] = {"hit": {"seen": 1, "emitted": 0}}
            h.manager.methods.update({m: {"name": m} for m in ("Coverage.start", "Coverage.report", "GameEvents.counts")})
            started = json.loads(await h.text("coverage_start", plugins=["Mod.esp"]))
            self.assertTrue(started["started"])
            self.assertIn(("Coverage.start", {"plugins": ["Mod.esp"]}), sock.calls)
            h.manager._on_event({"type": "papyrusError", "seq": 1, "epoch": 0,
                                 "data": {"severity": "error", "text": "boom", "script": "S", "form": None}})
            report = json.loads(await h.text("coverage_report", stop=True))
            self.assertIn("Quest stages", report["markdown"])
            lines = [json.loads(line) for line in h.recorder.path.read_text().splitlines()]
            self.assertTrue(any(line["t"] == "papyrusError" and line["text"] == "boom" for line in lines))
            self.assertTrue(any(line["t"] == "coverage" for line in lines))
            await h.text("game_stop")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
