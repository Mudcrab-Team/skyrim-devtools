import base64
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

from skyrim_devtools import profile_sqlite, profile_trace, profiling

from test_profiling import gpu_raw, write_symbols

FREQ = 10_000_000
ORIGIN = 5_000_000


def ticks(ms):
    return ORIGIN + int(ms * FREQ / 1000)


def event(start_ms, ms, name, flags=0, a=0, b=0):
    return struct.pack("<qIIQQ", ticks(start_ms), int(ms * FREQ / 1000), name | (flags << 20), a, b)


NAMES = [["Main::RenderWorld", "render"], ["Present", "d3d11"], ["pass frame", "pass"], ["pass rtv 1920x1080", "pass"],
         ["DrawIndexed", "d3d11"], ["WaitForSingleObjectEx", "wait"], ["ReadFile archive", "io"], ["TES::SetCurrentCell", "cell"]]


def traced_raw(api="batched"):
    raw = gpu_raw()
    raw["pid"] = 4242
    render = b"".join([
        event(0.5, 8.0, 0),                                             # engine phase in frame 1
        event(1.0, 2.0, 4, a=36, b=1),                                  # a draw inside it
        event(1.5, 5.0, 2, flags=0, a=10, b=(250 << 32)),               # pass 0 of frame 1 overlaps the draw's end
        event(14.0, 0.5, 1, a=0),                                        # Present of frame 0 -> passes after are frame 1
        event(14.6, 0.8, 2, flags=0, a=3 | (2 << 32), b=1 | (120 << 32)),
        event(15.4, 0.4, 3, flags=2, a=7, b=(2 << 16) | (90 << 32)),
        event(20.0, 40.0, 7),                                            # cell change in frame 2
        event(21.0, 30.0, 0),                                            # nested phase
        event(147.0, 0.2, 1, a=1),                                       # Present of frame 1 -> frame 2's passes
        event(147.5, 0.3, 2, flags=0, a=2),
        event(148.0, 0.4, 3, flags=1, a=4),
    ])
    worker = b"".join([event(2.0, 3.0, 5, a=0xFFFFFFFF, b=0), event(30.0, 1.0, 6, a=65536, b=1 | 0x100)])
    raw["trace"] = {
        "version": 1, "freq": FREQ, "originTicks": ORIGIN, "names": NAMES,
        "settings": {"api": api, "os": True, "waitMinUs": 20.0, "maxEvents": 2000000},
        "threads": [{"tid": 1, "name": "", "count": 8, "dropped": 0, "shortWaits": 5, "events": base64.b64encode(render).decode()},
                    {"tid": 7, "name": "IO worker", "count": 2, "dropped": 1, "shortWaits": 0, "events": base64.b64encode(worker).decode()}],
        "hooks": [{"label": "Main::RenderWorld", "rva": 4096, "calls": 2}, {"label": "Missing", "error": "not a function start"}],
        "api": {"installed": True, "calls": 9, "frames": 2}, "osHooks": {"patched": 9},
    }
    return raw


def read_varint(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def fields(data):
    """Top-level protobuf fields as [(number, wire, value)]."""
    out, pos = [], 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = read_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise AssertionError(f"wire type {wire}")
        out.append((number, wire, value))
    return out


def trace_processor_python():
    """A Python with the `perfetto` package for end-to-end checks (SKYRIM_DEVTOOLS_PERFETTO_PYTHON), or None."""
    path = os.environ.get("SKYRIM_DEVTOOLS_PERFETTO_PYTHON")
    return path if path and Path(path).exists() else None


class DecodeTests(unittest.TestCase):
    def test_events_on_profile_clock(self):
        decoded = profile_trace.decode(traced_raw())
        render = decoded["threads"][0]["events"]
        self.assertAlmostEqual(render[0][0], 0.5)
        self.assertAlmostEqual(render[0][1], 8.0)
        flags = [e[3] for e in render if decoded["names"][e[2]][1] == "pass"]
        self.assertEqual(sorted(flags), [0, 0, 0, 1, 2])

    def test_pass_frames_follow_present(self):
        decoded = profile_trace.decode(traced_raw())
        events = decoded["threads"][0]["events"]
        frames = profile_trace.pass_frames(events, decoded["names"])
        self.assertEqual(sorted(frames.values()), [1, 1, 2, 2])

    def test_untraced_profile(self):
        self.assertIsNone(profile_trace.decode(gpu_raw()))
        self.assertFalse(profile_trace.summary(gpu_raw())["traced"])


class PerfettoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.profile = profiling.Profile(traced_raw(), profiling.GameSymbols(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_packets_and_tracks(self):
        data, stats = profile_trace.build_perfetto(self.profile)
        packets = [fields(v) for n, w, v in fields(data)]
        self.assertTrue(all(n == 1 for n, _, _ in fields(data)))
        names = set()
        for packet in packets:
            for number, _, value in packet:
                if number == 60:
                    for f, _, v in fields(value):
                        if f == 2:
                            names.add(v.decode())
        for expected in ("GPU", "GPU passes", "D3D11 passes", "D3D11 API", "Markers / Agent", "Counters", "hitches"):
            self.assertIn(expected, names)
        self.assertEqual(stats["flows"], 2)
        self.assertGreater(stats["samples"], 0)
        self.assertEqual(stats["threads"], 2)

    def test_nest_clips_misnested_slices(self):
        writer = profile_trace.PerfettoWriter(1)
        clipped = writer.nest(5, [(100, 200, "outer", None, {}, None, None), (150, 260, "late", None, {}, None, None),
                                  (300, 300, "zero", None, {}, None, None)])
        self.assertEqual(clipped, 1)
        ends = sorted(ts for ts, order, _, _ in writer.events if order == 0)
        self.assertEqual(ends, [200, 200, 301])

    def test_slices_balance_per_track(self):
        data, _ = profile_trace.build_perfetto(self.profile)
        depth = {}
        last_ts = 0
        for _, _, packet in fields(data):
            parsed = dict((n, v) for n, _, v in fields(packet))
            if 11 not in parsed:
                continue
            self.assertGreaterEqual(parsed[8], last_ts)
            last_ts = parsed[8]
            event = dict((n, v) for n, _, v in fields(parsed[11]))
            track = event[11]
            if event[9] == 1:
                depth[track] = depth.get(track, 0) + 1
            elif event[9] == 2:
                depth[track] -= 1
                self.assertGreaterEqual(depth[track], 0)
        self.assertTrue(all(v == 0 for v in depth.values()))

    def test_chrome_json_fallback(self):
        target = self.root / "trace.json"
        stats = profile_trace.write_chrome_json(self.profile, target)
        events = json.loads(target.read_text())["traceEvents"]
        self.assertEqual(stats["events"], len(events))
        self.assertIn("Main::RenderWorld", {e["name"] for e in events})

    def test_save_outputs_writes_trace(self):
        paths, _ = profiling.save_outputs(self.profile, self.root / "out")
        self.assertTrue(Path(paths["perfetto"]).stat().st_size > 0)

    @unittest.skipUnless(trace_processor_python(), "set SKYRIM_DEVTOOLS_PERFETTO_PYTHON to a Python with perfetto")
    def test_trace_processor_loads_it(self):
        target = self.root / "trace.pftrace"
        profile_trace.write_perfetto(self.profile, target)
        script = (
            "import json, sys\n"
            "from perfetto.trace_processor import TraceProcessor\n"
            "tp = TraceProcessor(trace=sys.argv[1])\n"
            "q = lambda s: [list(r.__dict__.values()) for r in tp.query(s)]\n"
            "print(json.dumps({'slices': q('select name, count(*) from slice group by name order by name'),\n"
            "  'threads': q('select tid, name from thread where tid in (1, 7) order by tid'),\n"
            "  'flows': q('select count(*) from flow'), 'samples': q('select count(*) from cpu_profile_stack_sample'),\n"
            "  'counters': q('select count(*) from counter'),\n"
            "  'errors': q(\"select name from stats where value > 0 and severity != 'info'\"),\n"
            "  'nested': q(\"select count(*) from slice where name = 'DrawIndexed' and depth = 0\")}))\n"
        )
        out = subprocess.run([trace_processor_python(), "-c", script, str(target)], capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        slices = dict(result["slices"])
        self.assertEqual(slices["Main::RenderWorld"], 2)
        self.assertEqual(slices["pass rtv 1920x1080"], 2)
        self.assertEqual(result["threads"][-1], [7, "IO worker"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["flows"], [[2]])
        self.assertGreater(result["samples"][0][0], 0)
        self.assertGreater(result["counters"][0][0], 0)


class SummaryAndSqliteTests(unittest.TestCase):
    def test_summary(self):
        summary = profile_trace.summary(traced_raw())
        phases = {p["phase"]: p for p in summary["phases"]}
        self.assertIn("Main::RenderWorld", phases)
        self.assertEqual(phases["Main::RenderWorld"]["calls"], 2)
        self.assertEqual(summary["droppedEvents"], 1)
        self.assertEqual(summary["drawsPerFrame"]["max"], 20)
        self.assertEqual(summary["reads"][0]["read"], "ReadFile archive")
        self.assertEqual(summary["waits"][0]["thread"], "IO worker")
        self.assertEqual(summary["hooks"][1]["error"], "not a function start")

    def test_sqlite_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "p.sqlite"
            counts = profile_sqlite.export_sqlite(profiling.Profile(traced_raw()), target)
            self.assertEqual(counts["TraceEvents"], 13)
            rows = profile_sqlite.query(target, "SELECT name, passIndex, a FROM TraceEventsV WHERE category = 'pass' ORDER BY start")
            self.assertEqual(rows["rows"][-1], ["pass rtv 1920x1080", 1, 4])
            db = sqlite3.connect(target)
            try:
                self.assertIn("Missing", db.execute("SELECT value FROM Meta WHERE key = 'traceHooks'").fetchone()[0])
            finally:
                db.close()

    def test_documented_recipes_run(self):
        doc = (Path(__file__).resolve().parent.parent / "docs" / "profiling.md").read_text(encoding="utf-8")
        section = doc[doc.index("## Perfetto trace"):doc.index("## SQLite export")]
        blocks = [b.split("```")[0] for b in section.split("```sql\n")[1:]]
        self.assertEqual(len(blocks), 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "p.sqlite"
            profile_sqlite.export_sqlite(profiling.Profile(traced_raw()), target)
            results = [profile_sqlite.query(target, sql) for sql in blocks]
        self.assertEqual(results[0]["rows"][0][2], "TES::SetCurrentCell")
        self.assertEqual(results[1]["rows"][0][0], "TES::SetCurrentCell")
        self.assertEqual(results[2]["rows"][0][2], 20)


class HookListTests(unittest.TestCase):
    def test_local_names_resolve_against_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_symbols(root)
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"hooks": [{"name": "Alpha::Run", "category": "job"}, {"name": "Nope"}, {"id": 5, "label": "x"}]}))
            found, unresolved = profile_trace.local_hooks([hooks, root / "missing.json"], profiling.GameSymbols(root))
            self.assertEqual(found[0], {"category": "job", "rva": 0x1000, "label": "Alpha::Run"})
            self.assertEqual(unresolved, ["Nope"])
            config = profile_trace.trace_config(hooks=found)
            self.assertEqual(len(config["hooks"]), len(profile_trace.DEFAULT_HOOKS) + 2)

    def test_export_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            import gzip

            with gzip.open(out / "raw.json.gz", "wt") as handle:
                json.dump(traced_raw(), handle)
            for name in ("t.pftrace", "t.json"):
                code = profiling.main(["export", str(out), "--perfetto", str(root / name), "--symbols", str(root)])
                self.assertEqual(code, 0)
                self.assertTrue((root / name).stat().st_size > 0)


if __name__ == "__main__":
    sys.exit(unittest.main())
