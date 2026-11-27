import re
import shutil
import subprocess
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent / "windows" / "host.ps1"
TEXT = HOST.read_text()
PWSH = shutil.which("pwsh")


def function_body(name):
    start = TEXT.index(f"function {name}")
    depth = 0
    for index in range(TEXT.index("{", start), len(TEXT)):
        depth += {"{": 1, "}": -1}.get(TEXT[index], 0)
        if depth == 0:
            return TEXT[start:index + 1]
    raise AssertionError(name)


class ProfileLifecycleStructure(unittest.TestCase):
    def test_preflight_enters_before_touching_the_profile(self):
        body = function_body("Preflight")
        self.assertLess(body.index("Enter-IsolatedProfile"), body.index("Assert-LocalProfile"))
        self.assertLess(body.index("already running"), body.index("Enter-IsolatedProfile"))

    def test_run_always_leaves_after_the_game_is_gone(self):
        body = function_body("Run-Game")
        final = body.rindex("finally")
        self.assertIn("Exit-IsolatedProfile", body[final:])
        self.assertLess(body.index("$game.Kill()"), body.index("Exit-IsolatedProfile"))
        self.assertLess(body.index("Exit-IsolatedProfile"), body.rindex("Remove-Item -LiteralPath $lease"))

    def test_stale_host_reset_restores_the_profile(self):
        self.assertIn("Exit-IsolatedProfile", function_body("Reset-StaleHost"))

    def test_start_refuses_an_inactive_logon_session_before_launching(self):
        start = TEXT.index("    'start' {")
        body = TEXT[start:TEXT.index("    'health' {", start)]
        self.assertLess(body.index("Get-LogonSession"), body.index("Start-ScheduledTask"))
        self.assertIn("'logon'", TEXT[:TEXT.index("[string] $Action")])
        self.assertIn("'Disc'", function_body("Get-LogonSession"))

    def test_start_refuses_busy_task_and_foreign_steam_before_launching(self):
        start = TEXT.index("    'start' {")
        body = TEXT[start:TEXT.index("    'health' {", start)]
        self.assertIn("Interactive task is busy", body)
        self.assertLess(body.index("Get-ForeignSteam"), body.index("Start-ScheduledTask"))
        self.assertIn("steam-other-account", function_body("Assert-OfflineSteam"))
        self.assertIn("GetOwner", function_body("Get-ForeignSteam"))

    def test_restore_action_exists(self):
        self.assertIn("'restore-profile'", TEXT)

    def test_exit_only_removes_the_link_never_recursively(self):
        body = function_body("Exit-IsolatedProfile")
        self.assertIn("cmd.exe /c rmdir", body)
        self.assertNotIn("-Recurse", body)
        self.assertIn("game-running", body)

    def test_nothing_in_the_lifecycle_deletes_the_real_folder(self):
        for name in ("Enter-IsolatedProfile", "Exit-IsolatedProfile", "Get-EnterPlan", "Get-DocsLinkState"):
            self.assertNotRegex(function_body(name), r"Remove-Item|\[IO\.Directory\]::Delete")


@unittest.skipUnless(PWSH, "PowerShell is not installed")
class EnterPlan(unittest.TestCase):
    def plan(self, state, backup):
        script = function_body("Get-EnterPlan") + f"\ntry {{ Get-EnterPlan '{state}' ${backup} }} catch {{ 'ERROR' }}"
        out = subprocess.run([PWSH, "-NoProfile", "-Command", script], capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_decision_table(self):
        cases = {
            ("missing", "false"): "link", ("missing", "true"): "link",
            ("junction-profile", "true"): "reuse", ("junction-profile", "false"): "reuse",
            ("real", "false"): "backup-and-link", ("real", "true"): "ERROR",
            ("junction-other", "false"): "ERROR", ("file", "false"): "ERROR",
        }
        for (state, backup), expected in cases.items():
            with self.subTest(state=state, backup=backup):
                self.assertEqual(self.plan(state, backup), expected)


if __name__ == "__main__":
    unittest.main()
