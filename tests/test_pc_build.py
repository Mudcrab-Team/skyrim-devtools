import importlib.machinery
import importlib.util
import os
import signal
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


class LeaseTests(unittest.TestCase):
    def test_hold_takes_the_lock_as_a_lease(self):
        calls = []
        with mock.patch.object(pc_build, "run_remote", lambda host, args: calls.append(args) or 0):
            pc_build.hold(FakeHost(), options(1), [sys.executable, "-c", "pass"])
        self.assertIn("-Action lock", calls[0])
        self.assertIn(" -Lease", calls[0])

    def test_heartbeat_outpaces_the_remote_staleness_window(self):
        self.assertLessEqual(pc_build.HEARTBEAT_SECONDS * 3, 90)

    def hold_with_refresh_codes(self, codes):
        scripts = []
        codes = list(codes)

        class Host(FakeHost):
            def run_powershell(self, script, **kwargs):
                scripts.append(script)
                return types.SimpleNamespace(returncode=codes.pop(0) if codes else 0, stderr="")

        with mock.patch.object(pc_build, "run_remote", lambda host, args: 0), \
                mock.patch.object(pc_build, "HEARTBEAT_SECONDS", 0.01):
            status = pc_build.hold(Host(), options(1), [sys.executable, "-c", "import time; time.sleep(0.5)"])
        return status, scripts

    def test_heartbeats_keep_flowing_while_the_command_runs(self):
        status, scripts = self.hold_with_refresh_codes([])
        self.assertEqual(status, 0)
        self.assertGreater(len(scripts), 2)
        self.assertTrue(all("-Refresh" in s for s in scripts))

    def test_a_network_failure_on_a_heartbeat_does_not_mean_the_lock_was_lost(self):
        status, _ = self.hold_with_refresh_codes([255, 255, 0])
        self.assertEqual(status, 0)

    def test_a_lock_taken_over_by_someone_else_fails_the_hold(self):
        status, _ = self.hold_with_refresh_codes([4])
        self.assertEqual(status, 125)


class QueuedHoldTests(unittest.TestCase):
    def run_hold(self, wait):
        calls = []
        scripts = []

        class Host:
            def run_powershell(self, script, **kwargs):
                scripts.append(script)
                # a queued agent has no lock to refresh, so the remote answers 4 until the wait ends
                return types.SimpleNamespace(returncode=0 if waited else 4, stderr="")

        waited = []

        def remote(host, args):
            calls.append(args)
            code = wait(args)
            if "-Action lock" in args:
                waited.append(True)
            return code

        with mock.patch.object(pc_build, "run_remote", remote), mock.patch.object(pc_build, "HEARTBEAT_SECONDS", 0.01), \
                mock.patch.object(pc_build, "RETRY_DELAY", 0):
            try:
                status = pc_build.hold(Host(), options(1), [sys.executable, "-c", "pass"])
            except SystemExit as exit_:
                status = exit_.code
        return status, calls, scripts

    def test_queue_ticket_is_pinged_while_waiting_and_not_mistaken_for_a_lost_lock(self):
        def wait(args):
            if "-Action lock" in args:
                time.sleep(0.2)
            return 0

        status, _, scripts = self.run_hold(wait)
        self.assertEqual(status, 0)
        self.assertTrue(scripts and all("-Refresh" in s for s in scripts))

    def test_killed_while_queued_still_releases_so_the_ticket_is_removed(self):
        def wait(args):
            if "-Action lock" in args:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
            return 0

        status, calls, _ = self.run_hold(wait)
        self.assertEqual(status, 128 + signal.SIGTERM)
        self.assertEqual(len([c for c in calls if "-Action unlock" in c]), 1)

    def test_failed_wait_also_releases(self):
        status, calls, _ = self.run_hold(lambda args: 4 if "-Action lock" in args else 0)
        self.assertEqual(status, 4)
        self.assertEqual(len([c for c in calls if "-Action unlock" in c]), 1)


class RemoteScriptTests(unittest.TestCase):
    """The lock logic is PowerShell and cannot run on the Mac; these pin the rules the Mac side relies on."""

    script = (SOURCE.parent / "pc_build_remote.ps1").read_text()

    def test_stale_lease_is_taken_over_and_logged(self):
        self.assertIn("$leaseSeconds = 90", self.script)
        self.assertIn("if (Test-LeaseLock) { return (Get-LockAge) -gt $leaseSeconds }", self.script)
        self.assertIn("is stale (no heartbeat for", self.script)

    def test_locks_without_a_lease_keep_the_slow_rule(self):
        self.assertIn("(Get-LockAge) -gt (25 * 60)) -and -not (Get-Process SkyrimSE", self.script)

    def test_stale_queue_entries_are_pruned_and_logged(self):
        self.assertIn("$ticketTtlSeconds = $leaseSeconds", self.script)
        self.assertIn("dropping stale queue entry", self.script)

    def test_orphaned_waiter_gives_up(self):
        self.assertIn("Test-ParentAlive", self.script)

    def test_orphan_check_watches_the_whole_session_chain(self):
        self.assertIn("function Get-ParentChain", self.script)
        self.assertIn("foreach ($entry in $chain)", self.script)

    def test_leased_waiter_does_not_refresh_its_own_ticket(self):
        self.assertIn("if (-not $Lease) { (Get-Item $ticket).LastWriteTime = Get-Date }", self.script)
        self.assertIn("if ($Lease -and $queued -and (-not $mine", self.script)
        self.assertIn("-gt $pingGraceSeconds", self.script)

    def test_refresh_pings_a_queued_ticket_and_unlock_removes_it(self):
        self.assertIn("'ticket refreshed'", self.script)
        unlock = self.script[self.script.index("if ($Action -eq 'unlock')"):]
        self.assertLess(unlock.index('.ticket'), unlock.index("Remove-Lock"))

    def test_queued_waiter_takes_over_a_stale_lock_itself(self):
        loop = self.script[self.script.index("function Acquire-Lock"):]
        self.assertLess(loop.index("while ($true)"), loop.index("Test-LockStale"))
        self.assertLess(loop.index("Test-LockStale"), loop.index("Start-Sleep"))


if __name__ == "__main__":
    unittest.main()
