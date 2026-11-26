import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from skyrim_devtools import profile_io

FREQ = 1_000_000  # one tick per microsecond keeps the toy timings readable
BASE = 5_000_000


def ev(t, dur, op, file=0, offset=0, size=0, result=0, stack=0, thread=1, flags=0, extra=0):
    return profile_io.EVENT.pack(BASE + int(t * FREQ), int(dur * FREQ), file, offset, size, result, stack, thread,
                                 flags, op, extra)


def toy():
    files = [["", ""],
             ["Data\\Skyrim.esm", "C:\\Game\\Data\\Skyrim.esm"],
             ["Data\\Mod.esp", "C:\\MO\\mods\\Some Mod\\Mod.esp"],
             ["Data\\Big - Textures.bsa", "C:\\MO\\mods\\Tex Pack\\Big - Textures.bsa"],
             ["C:\\Game\\Data\\SKSE\\Plugins\\Foo.dll", "C:\\Game\\Data\\SKSE\\Plugins\\Foo.dll"]]
    blob = b"".join([
        ev(0.5, 0.01, 10, file=4),                                   # LoadLibrary Foo.dll
        ev(0.6, 0.2, 11, file=4),                                    # SKSEPlugin_Load Foo.dll
        ev(1.0, 0.001, 1, file=1, offset=1000),                      # open Skyrim.esm
        ev(1.1, 0.010, 2, file=1, offset=0, size=100, result=100, stack=1),
        ev(1.2, 0.010, 2, file=1, offset=100, size=100, result=100, stack=1),   # sequential
        ev(1.3, 0.020, 2, file=1, offset=900, size=100, result=100, stack=1),   # jump
        ev(2.0, 0.001, 1, file=2, offset=500),
        ev(2.1, 0.050, 2, file=2, offset=0, size=500, result=500, stack=1),
        ev(2.5, 0.001, 5, file=3, offset=4096, result=0),            # seek
        ev(5.0, 0.002, 1, file=3, offset=1 << 30),
        ev(5.1, 0.300, 2, file=3, offset=4096, size=65536, result=65536, stack=2, thread=2),
        ev(5.2, 0.100, 2, file=3, offset=1 << 20, size=1 << 20, result=1 << 20, stack=2, thread=2),
        ev(5.3, 0.010, 2, file=3, offset=0, size=64, result=64, stack=2, thread=2, flags=profile_io.FLAG_NESTED),
        b"\0" * profile_io.EVENT.size,                               # unwritten slot
    ])
    meta = {
        "format": 1, "eventSize": 48, "events": 14, "dropped": 0, "qpcFrequency": FREQ, "armedAt": BASE,
        "armedEpochMs": 1.0, "hookOverheadS": 0.01, "hooks": {"ReadFile": "ok", "CreateFile2": "missing"},
        "files": files, "threads": [0, 111, 222], "threadNames": {"111": "main", "222": "loader"},
        "gameBase": 0x140000000,
        "modules": [{"path": "C:\\Game\\SkyrimSE.exe", "base": 0x140000000, "size": 0x1000000},
                    {"path": "C:\\Windows\\System32\\KERNELBASE.dll", "base": 0x7ff000000000, "size": 0x1000},
                    {"path": "C:\\Game\\Data\\SKSE\\Plugins\\!SkyrimDevTools.dll", "base": 0x180000000, "size": 0x1000},
                    {"path": "C:\\Game\\Data\\SKSE\\Plugins\\Foo.dll", "base": 0x190000000, "size": 0x1000}],
        "stacks": [[], [0, 1, 2], [0, 1, 3, 2]],
        "frames": [{"module": 2, "rva": 0x10}, {"module": 1, "rva": 0x20}, {"module": 0, "rva": 0x5010, "id": 77, "idOffset": 0x10},
                   {"module": 3, "rva": 0x40, "name": "Reader::Pull", "offset": 4}],
        "marks": [[BASE + 100, "iotrace armed"], [BASE + 3 * FREQ, "skse PostLoad"], [BASE + int(3.5 * FREQ), "skse InputLoaded"],
                  [BASE + 4 * FREQ, "skse DataLoaded"], [BASE + int(4.5 * FREQ), "Main Menu open"],
                  [BASE + 5 * FREQ, "coc Riverwood begin"], [BASE + 6 * FREQ, "coc Riverwood end"]],
        "counterColumns": ["at", "pageFaults", "workingSet", "privateBytes", "availPhys", "readOps", "readBytes", "otherOps",
                           "sysPageReads", "sysPageReadIos", "sysTransition", "sysDemandZero", "sysCacheTransition", "sysFaults"],
        "counters": [[BASE + int(t * FREQ), 100 * t, 1000 + t, 0, 5000 - t, t, 4096 * t, 0, (2**32 - 5 + 10 * t) % 2**32, 0, 0, 0, 0, 0]
                     for t in range(0, 7)],
        "threadSamples": [[BASE + int(t * FREQ), 111, 0, int(t * 0.5 * 1e7)] for t in range(0, 7)]
                         + [[BASE + int(t * FREQ), 222, 0, int(t * 0.1 * 1e7)] for t in range(0, 7)],
    }
    return meta, blob


class IoTraceTest(unittest.TestCase):
    def trace(self):
        meta, blob = toy()
        return profile_io.IoTrace(meta, blob)

    def test_parses_and_skips_unwritten(self):
        trace = self.trace()
        self.assertEqual(len(trace.events), 13)
        self.assertAlmostEqual(trace.events[0].t, 0.5)
        self.assertEqual(trace.events[0].op, 10)

    def test_phases_from_marks(self):
        names = {p[0]: (p[1], p[2]) for p in self.trace().phases}
        self.assertEqual(names["startup"], (0.0, 4.5))
        self.assertEqual(names["skse-plugins"], (0.0, 3.0))
        self.assertEqual(names["data-load"], (3.5, 4.0))
        self.assertEqual(names["load Riverwood"], (5.0, 6.0))

    def test_callers_skip_hook_and_os_frames(self):
        trace = self.trace()
        self.assertEqual(trace.callers[1][0], "id:77+0x10")
        self.assertEqual(trace.callers[2][0], "Foo.dll!Reader::Pull")
        self.assertEqual(trace.callers[2][2][-1], "id:77+0x10")

    def test_file_stats_sequential_and_small(self):
        trace = self.trace()
        stats = trace.file_stats(trace.events)
        esm = stats[1]
        self.assertEqual(esm["reads"], 3)
        self.assertEqual(esm["nonSequential"], 1)
        self.assertEqual(esm["smallReads"], 3)
        self.assertEqual(esm["bytes"], 300)
        bsa = stats[3]
        self.assertEqual(bsa["reads"], 2)  # the nested read is not counted twice
        self.assertEqual(bsa["seeks"], 1)

    def test_summary_phase_counts_and_counters(self):
        summary = self.trace().summary()
        load = next(p for p in summary["phases"] if p["name"] == "load Riverwood")
        self.assertEqual(load["reads"], 2)
        self.assertAlmostEqual(load["readS"], 0.4, places=3)
        self.assertEqual(load["counters"]["pageFaults"], 100)
        self.assertEqual(load["counters"]["sysPageReads"], 10)  # across the 32-bit wrap
        self.assertEqual(load["topFiles"][0]["mod"], "Tex Pack")
        self.assertEqual(load["byKind"]["archive"]["reads"], 2)
        self.assertIn("65536", load["sizeHistogram"])
        cpu = {row[0]: row[2] for row in load["busiestCpuThreads"]}
        self.assertAlmostEqual(cpu[111], 0.5, places=2)

    def test_plugins_and_dlls(self):
        trace = self.trace()
        plugins = trace.plugin_times()
        self.assertEqual([p["plugin"] for p in plugins], ["Skyrim.esm", "Mod.esp"])
        self.assertAlmostEqual(plugins[0]["spanS"], 0.32, places=3)
        self.assertEqual(plugins[1]["mod"], "Some Mod")
        dlls = trace.dll_times()
        self.assertEqual(dlls[0]["dll"], "Foo.dll")
        self.assertAlmostEqual(dlls[0]["pluginLoadS"], 0.2, places=3)

    def test_outputs(self):
        trace = self.trace()
        with tempfile.TemporaryDirectory() as tmp:
            paths, report = profile_io.save_outputs(trace, tmp)
            self.assertIn("hooks not installed: CreateFile2", report)
            self.assertIn("Skyrim.esm", report)
            db = sqlite3.connect(paths["sqlite"])
            self.assertEqual(db.execute("SELECT count(*) FROM io_events").fetchone()[0], 13)
            self.assertEqual(db.execute("SELECT mod FROM io_files WHERE id = 3").fetchone()[0], "Tex Pack")
            self.assertEqual(db.execute("SELECT count(*) FROM io_plugins").fetchone()[0], 2)
            db.close()
            perfetto = json.loads(gzip.decompress(Path(paths["perfetto"]).read_bytes()))
            names = {e.get("name") for e in perfetto["traceEvents"]}
            self.assertIn("load Riverwood", names)
            self.assertIn("faults/s", names)

    def test_load_from_files(self):
        meta, blob = toy()
        with tempfile.TemporaryDirectory() as tmp:
            stem = Path(tmp) / "iotrace-1-final"
            stem.with_suffix(".json").write_text(json.dumps(meta))
            stem.with_suffix(".bin").write_bytes(blob)
            trace = profile_io.IoTrace.load(stem.with_suffix(".json"))
            self.assertEqual(len(trace.events), 13)
            self.assertEqual(profile_io.main([str(stem.with_suffix(".json")), "--out", str(Path(tmp) / "out")]), 0)
            self.assertTrue((Path(tmp) / "out" / "io.sqlite").exists())

    def test_rejects_other_event_size(self):
        meta, blob = toy()
        meta["eventSize"] = 40
        with self.assertRaises(ValueError):
            profile_io.IoTrace(meta, blob)


if __name__ == "__main__":
    unittest.main()
