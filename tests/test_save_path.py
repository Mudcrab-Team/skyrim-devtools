import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")
SESSION = (HERE.parent / "native" / "session.cpp").read_text()


@unittest.skipUnless(COMPILER, "no C++ compiler")
class SavePathRule(unittest.TestCase):
    def test_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "save_path_test"
            built = subprocess.run([COMPILER, "-std=c++20", "-o", str(binary), str(HERE / "save_path_test.cpp")],
                                   capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SessionUsesTheRule(unittest.TestCase):
    def body(self, name):
        start = SESSION.index(name)
        return SESSION[start:SESSION.index("\n    }\n", start)]

    def test_load_is_gated_by_the_rule(self):
        self.assertIn("savepath::Classify", self.body("savepath::Layout CurrentSaveLayout"))
        self.assertIn("Layout::Refused", self.body("void RequireIsolatedSavePath"))
        self.assertIn("RequireIsolatedSavePath();", self.body("void BeginLoad"))

    def test_listing_checks_an_mo2_folder_before_reading_it(self):
        listing = SESSION[SESSION.index('"Session.listSaves"'):SESSION.index('"Session.loadSave"')]
        self.assertLess(listing.index("__mo_saves"), listing.index("RequireIsolatedSavePath"))
        self.assertLess(listing.index("RequireIsolatedSavePath"), listing.index("directory_iterator"))


if __name__ == "__main__":
    unittest.main()
