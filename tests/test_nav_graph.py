import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


@unittest.skipUnless(COMPILER, "no C++ compiler")
class NavGraphTests(unittest.TestCase):
    def run_cpp(self, name):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / name
            built = subprocess.run([COMPILER, "-std=c++20", "-O1", "-o", str(binary), str(HERE / f"{name}.cpp")],
                                   capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_path_search(self):
        self.run_cpp("nav_graph_test")

    def test_global_route(self):
        self.run_cpp("nav_global_test")


if __name__ == "__main__":
    unittest.main()
