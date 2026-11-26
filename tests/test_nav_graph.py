import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


@unittest.skipUnless(COMPILER, "no C++ compiler")
class NavGraphTests(unittest.TestCase):
    def test_path_search(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "nav_graph_test"
            subprocess.run([COMPILER, "-std=c++20", "-O1", "-o", str(binary), str(HERE / "nav_graph_test.cpp")],
                           check=True, capture_output=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
