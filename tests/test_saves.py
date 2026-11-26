import json
import unittest

import test_use
from skyrim_devtools.use import saves
from test_use import Harness, run


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.methods = list(test_use.METHODS)
        test_use.METHODS.append("Session.listSaves")
        self.scripts = []

        async def fake_run(manager, script):
            self.scripts.append(script)
            return json.dumps({"kept": True})

        self.original = saves._run
        saves._run = fake_run

    def tearDown(self):
        saves._run = self.original
        test_use.METHODS[:] = self.methods

    def test_bad_names_are_refused_before_anything_runs(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            for name in ("../x", "a b", "", "Fixture", "x" * 41):
                with self.assertRaises(Exception):
                    await h.text("game_save", name=name)
            self.assertEqual(self.scripts, [])
            await h.text("game_stop")
        run(scenario())

    def test_save_waits_for_the_file_then_keeps_it(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Session.listSaves"] = [{"name": "gc4", "bytes": 1}]
            result = json.loads(await h.text("game_save", name="gc4"))
            self.assertTrue(result["kept"])
            self.assertIn(("Console.execute", {"command": "save gc4", "target": "player"}), h.sockets[0].calls)
            self.assertIn("fixtures", self.scripts[0])
            self.assertIn("gc4.ess", self.scripts[0])
            await h.text("game_stop")
        run(scenario())

    def test_keep_false_removes_the_kept_copy(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            await h.text("game_save", name="gc4", keep=False)
            self.assertIn("Remove-Item", self.scripts[0])
            await h.text("game_stop")
        run(scenario())


if __name__ == "__main__":
    unittest.main()
