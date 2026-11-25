import json
import tempfile
import unittest
from pathlib import Path

from skyrim_devtools import trajectory
from test_use import METHODS, Harness, run

NAV_METHODS = ["Move.walk", "Move.turnTo", "Camera.state", "Camera.set", "Camera.lookAt", "Nav.goto",
               "Nav.status", "Nav.cancel", "Record.start", "Record.stop", "Record.list", "Record.replayCamera"]


def sample(t, x, y, speed=100.0, frame_ms=16.0, **extra):
    return {"t": t, "pos": [x, y, 0.0], "speed": speed, "frameMs": frame_ms, **extra}


def write(folder, samples, name="run.jsonl"):
    path = Path(folder) / name
    path.write_text("\n".join(json.dumps(s) for s in samples) + "\nnot json\n\n", encoding="utf-8")
    return path


class TrajectoryTests(unittest.TestCase):
    def test_load_skips_noise_and_summarize(self):
        with tempfile.TemporaryDirectory() as folder:
            samples = [
                sample(0.0, 0, 0),
                sample(1.0, 300, 400, nav={"state": "recovering"}),
                sample(2.0, 300, 400, stuck=True),
                sample(3.0, 600, 800),
                sample(4.0, 50000, 50000, teleport=True, frameMs=100.0),
            ]
            loaded = trajectory.load(write(folder, samples))
        self.assertEqual(len(loaded), 5)
        summary = trajectory.summarize(loaded)
        self.assertEqual(summary["samples"], 5)
        self.assertEqual(summary["durationS"], 4.0)
        self.assertEqual(summary["distance"], 1000.0)  # the teleport hop is not walking
        self.assertEqual(summary["teleports"], 1)
        self.assertEqual(summary["stuckEpisodes"], 1)
        self.assertEqual(summary["start"], [0, 0, 0.0])
        self.assertEqual(summary["frameMs"]["max"], 100.0)

    def test_summarize_empty(self):
        self.assertEqual(trajectory.summarize([])["samples"], 0)

    def test_plot_svg_markers(self):
        samples = [sample(i, i * 100, (i % 3) * 50, speed=i * 10, stuck=(i == 4)) for i in range(10)]
        samples[6]["teleport"] = True
        svg = trajectory.plot_svg(samples, title="a<b")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<title>start</title>", svg)
        self.assertIn("<title>end</title>", svg)
        self.assertEqual(svg.count("<title>stuck</title>"), 1)
        self.assertEqual(svg.count("<line x1"), 9)  # 9 segments minus a teleport, plus the scale bar
        self.assertIn("a&lt;b", svg)
        self.assertIn("frame time", trajectory.plot_svg(samples, color_by="frame"))
        with self.assertRaises(ValueError):
            trajectory.plot_svg(samples[:1])
        with self.assertRaises(ValueError):
            trajectory.plot_svg(samples, color_by="height")

    def test_episodes_load_summary_and_plot(self):
        status = {"active": False, "last": {
            "stalls": {"recent": [
                {"kind": "stall", "atS": 3, "durationS": 1.2, "pos": [100, 0, 0], "cause": "door"},
                {"kind": "stall", "atS": 9, "durationS": 2.5, "pos": [300, 0, 0], "cause": "actor in the way: Wolf"},
                {"kind": "offPath", "atS": 5, "durationS": 4.0, "pos": [200, 50, 0], "maxDistance": 180}]},
            "interruptions": {"recent": [
                {"reason": "dialogue", "action": "close", "atS": 12, "durationS": 1.5, "pos": [500, 0, 0]}]}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            path.write_text(json.dumps(status), encoding="utf-8")
            episodes = trajectory.load_episodes(path)
            events = Path(folder) / "events.jsonl"
            events.write_text("\n".join(json.dumps(e) for e in episodes) + "\n", encoding="utf-8")
            self.assertEqual(len(trajectory.load_episodes(events)), 4)
            summary = trajectory.episode_summary(episodes)
            self.assertEqual(summary["stall"]["count"], 2)
            self.assertEqual(summary["stall"]["totalS"], 3.7)
            self.assertEqual(summary["stall"]["over2s"], 1)
            self.assertEqual(summary["stall"]["causes"], {"door": 1, "actor in the way": 1})
            self.assertEqual(summary["offPath"]["count"], 1)
            self.assertEqual(summary["interruption"], {"count": 1, "totalS": 1.5, "maxS": 1.5})
            samples = [sample(i, i * 100, 0) for i in range(8)]
            svg = trajectory.plot_svg(samples, episodes=episodes)
            self.assertEqual(svg.count("<circle"), 5)  # four episodes plus the start
            self.assertIn("dialogue (close)", svg)
            self.assertIn("stroke-dasharray", svg)
            recording = write(folder, samples)
            out = Path(folder) / "out.svg"
            self.assertEqual(trajectory.main(["plot", str(recording), "-o", str(out), "--episodes", str(path)]), 0)
            self.assertIn("rings:", out.read_text())

    def test_scp_command(self):
        command = trajectory.scp_command({"sshDestination": "pc", "sshConfig": "~/x.conf"},
                                         "C:\\Users\\u\\rec.jsonl", "out.jsonl")
        self.assertEqual(command[-2], "pc:/C:/Users/u/rec.jsonl")
        self.assertIn("-F", command)
        self.assertNotIn("~", command[command.index("-F") + 1])
        with self.assertRaises(ValueError):
            trajectory.scp_command({"sshDestination": "-oProxyCommand=x"}, "a", "b")

    def test_cli_plot_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write(folder, [sample(0, 0, 0), sample(1, 100, 0)])
            out = Path(folder) / "out.svg"
            self.assertEqual(trajectory.main(["plot", str(path), "-o", str(out)]), 0)
            self.assertTrue(out.read_text().startswith("<svg"))
            self.assertEqual(trajectory.main(["summary", str(path)]), 0)
            single = write(folder, [sample(0, 0, 0)], "one.jsonl")
            self.assertEqual(trajectory.main(["plot", str(single), "-o", str(out)]), 2)


class NavToolTests(unittest.TestCase):
    def setUp(self):
        for method in NAV_METHODS:
            if method not in METHODS:
                METHODS.append(method)

    def test_tools_forward_parameters(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.results["Move.walk"] = {"distance": 420.0}
            self.assertEqual(json.loads(await h.text("walk", duration_ms=2000)), {"distance": 420.0})
            await h.text("nav_goto", goal={"refID": "0001A2B3"}, mode="ai", timeout_s=60, on_dialogue="stop",
                         invulnerable=True)
            await h.text("camera_set", mode="third", zoom=0.5)
            calls = dict(socket.calls)
            self.assertEqual(calls["Move.walk"], {"direction": "forward", "durationMs": 2000})
            self.assertEqual(calls["Nav.goto"], {"goal": {"refID": "0001A2B3"}, "mode": "ai", "timeoutS": 60,
                                                 "onDialogue": "stop", "invulnerable": True})
            self.assertEqual(calls["Camera.set"], {"mode": "third", "zoom": 0.5})
            await h.text("game_stop")
        run(scenario())

    def test_nav_wait_returns_last_summary(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Nav.status"] = {"active": False, "last": {"state": "arrived", "stuckCount": 0}}
            result = json.loads(await h.text("nav_wait", timeout_s=5))
            self.assertEqual(result, {"done": True, "state": "arrived", "stuckCount": 0})
            await h.text("game_stop")
        run(scenario())

    def test_trajectory_plot_tool(self):
        async def scenario():
            h = Harness()
            with tempfile.TemporaryDirectory() as folder:
                path = write(folder, [sample(0, 0, 0), sample(1, 0, 100)])
                result = json.loads(await h.text("trajectory_plot", file=str(path)))
                self.assertTrue(Path(result["plot"]).exists())
                self.assertEqual(result["summary"]["distance"], 100.0)
        run(scenario())


if __name__ == "__main__":
    unittest.main()
