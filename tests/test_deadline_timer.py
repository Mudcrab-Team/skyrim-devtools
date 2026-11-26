import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("c++"), "no C++ compiler")
class DeadlineTimerTests(unittest.TestCase):
    def test_single_thread_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "deadline_timer_test"
            subprocess.run(["c++", "-std=c++20", "-pthread", "-I", str(ROOT / "native"),
                            str(ROOT / "tests" / "native" / "deadline_timer_test.cpp"), "-o", str(exe)],
                           check=True)
            done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
