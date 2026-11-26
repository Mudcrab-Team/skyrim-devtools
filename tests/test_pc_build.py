import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent.parent / "tools" / "pc-build"
loader = importlib.machinery.SourceFileLoader("pc_build_tool", str(SOURCE))
spec = importlib.util.spec_from_loader("pc_build_tool", loader)
pc_build = importlib.util.module_from_spec(spec)
loader.exec_module(pc_build)


class FakeHost:
    def run(self, command, check=True, stream=False):
        return types.SimpleNamespace(returncode=0, stderr="")


def options(max_minutes):
    return types.SimpleNamespace(name="tester", lock_wait=0, max_minutes=max_minutes)


class HoldTests(unittest.TestCase):
    def hold(self, argv, max_minutes, unlock_codes=(0,)):
        calls = []
        codes = list(unlock_codes)

        def remote(host, args):
            calls.append(args)
            return codes.pop(0) if "unlock" in args and codes else 0

        with mock.patch.object(pc_build, "run_remote", remote), mock.patch.object(pc_build, "RETRY_DELAY", 0):
            status = pc_build.hold(FakeHost(), options(max_minutes), argv)
        return status, calls

    def test_timeout_releases_the_lock_and_kills_the_command(self):
        started = time.monotonic()
        status, calls = self.hold([sys.executable, "-c", "import time; time.sleep(60)"], 0.03)
        self.assertEqual(status, 124)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual([c for c in calls if "unlock" in c], [f"-Action unlock -Agent tester.{os.getpid()} -LockWait 0"])

    def test_child_exit_releases_the_lock_with_its_status(self):
        status, calls = self.hold([sys.executable, "-c", "raise SystemExit(3)"], 1)
        self.assertEqual(status, 3)
        self.assertEqual(len([c for c in calls if "unlock" in c]), 1)

    def test_each_hold_locks_under_its_own_name_and_passes_it_down(self):
        script = f"import os, sys; sys.exit(0 if os.environ.get('{pc_build.HELD_ENV}') == 'tester.{os.getpid()}' else 7)"
        status, calls = self.hold([sys.executable, "-c", script], 1)
        self.assertEqual(status, 0)
        self.assertIn(f"-Agent tester.{os.getpid()} ", calls[0])
        with mock.patch.dict(os.environ, {pc_build.HELD_ENV: "tester.42"}):
            self.assertEqual(pc_build.agent_name("tester"), "tester.42")
            self.assertEqual(pc_build.agent_name("other"), "other")

    def test_a_failed_release_is_retried(self):
        status, calls = self.hold([sys.executable, "-c", "pass"], 1, unlock_codes=(255, 255, 0))
        self.assertEqual(status, 0)
        self.assertEqual(len([c for c in calls if "unlock" in c]), 3)

    def test_a_lock_owned_by_someone_else_is_not_retried(self):
        status, calls = self.hold([sys.executable, "-c", "pass"], 1, unlock_codes=(4,))
        self.assertEqual(len([c for c in calls if "unlock" in c]), 1)

    def test_interrupt_while_waiting_still_releases(self):
        calls = []

        def remote(host, args):
            calls.append(args)
            return 0

        with mock.patch.object(pc_build, "run_remote", remote), \
                mock.patch.object(pc_build, "supervise", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pc_build.hold(FakeHost(), options(1), ["x"])
        self.assertTrue(any("unlock" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
