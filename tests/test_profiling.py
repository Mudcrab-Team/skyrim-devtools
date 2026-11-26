import asyncio
import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.parse
import zlib

from skyrim_devtools import profile_views, profiling


def write_symbols(root):
    pdb = root / "reconstruction" / "generated" / "crashlogger_pdb"
    (pdb / "names").mkdir(parents=True)
    (pdb / "boundaries").mkdir(parents=True)
    (pdb / "names" / "00.tsv").write_text("# toy\n0x1000\tAlpha::Run\n", encoding="utf-8")
    (pdb / "boundaries" / "00.tsv").write_text("# toy\n1000\t100\n2000\t80\n3000\t40\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "progress.json").write_text(json.dumps({"functions": [
        {"rva": 0x2000, "name": "Beta::Tick", "name_origin": "GUESS"}]}), encoding="utf-8")
    (root / "data" / "symbol-alignment.json").write_text(json.dumps({"functions": {
        "0x3000": {"name": "Gamma::Wait", "source": "toy-map"}}}), encoding="utf-8")


MODULES = [
    {"name": "SkyrimSE.exe", "path": "C:\\Games\\SkyrimSE.exe", "base": 0, "size": 0x100000, "game": True},
    {"name": "ntdll.dll", "path": "C:\\Windows\\System32\\ntdll.dll", "base": 0, "size": 0x1000, "game": False},
    {"name": "SkyrimDevTools.dll", "path": "C:\\Games\\Data\\SKSE\\Plugins\\SkyrimDevTools.dll", "base": 0,
     "size": 0x1000, "game": False},
]

FRAMES = [
    {"m": 0, "rva": 0x1010, "start": 0x1000},                          # 0 Alpha::Run
    {"m": 0, "rva": 0x2010, "start": 0x2000},                          # 1 Beta::Tick (guess)
    {"m": 0, "rva": 0x3004, "start": 0x3000},                          # 2 Gamma::Wait (aligned)
    {"m": 0, "rva": 0x5010, "start": 0x5000, "id": 77, "idOff": 0x10},  # 3 no name: Address Library id
    {"m": 1, "rva": 0x10, "sym": "NtWaitForSingleObject"},              # 4 wait leaf
    {"m": 2, "rva": 0x20, "sym": "devtools::Pump"},                     # 5 plugin
]

# stack index: [prefix, frame]; a stack is its leaf entry
STACKS = [
    [None, 0],  # 0 Alpha
    [0, 1],     # 1 Alpha > Beta
    [1, 3],     # 2 Alpha > Beta > id:77
    [0, 2],     # 3 Alpha > Gamma
    [3, 4],     # 4 Alpha > Gamma > NtWait
    [0, 5],     # 5 Alpha > plugin
]


def raw_profile(runs, hz=1000.0):
    return {"format": "skyrim-devtools-profile", "version": 1, "hz": hz, "threadsSpec": "main", "durationS": 1.0,
            "samplerCpuMs": 5.0, "frames": FRAMES, "stacks": STACKS, "modules": MODULES,
            "threads": [{"tid": 1, "name": "", "main": True, "frameThread": True, "samples": 10, "walked": 8,
                         "suspendedMs": 10.0, "runs": runs}],
            "frameMarks": [[0.0, 16.0, 1], [16.0, 150.0, 2]], "hitchThresholdMs": 100.0,
            "hitches": [{"frame": 2, "ms": 134.0, "startMs": 16.0, "stacks": [[3, 1, 0], [4, 2, 0]],
                         "samples": [[0.0, 0, 0], [1.0, 1, 1], [2.0, 1, 1]], "waitChain": None}],
            "hitchFrames": FRAMES}


RUNS = [[0.0, 2, 4, 0], [4.0, 1, 2, 0], [6.0, 4, 3, 1], [9.0, 5, 1, 0]]


def mszip_encode(data, block=32768):
    """The Windows Compression API's MSZIP buffer layout, for decoder tests."""
    out = bytearray(bytes.fromhex("0a51e5c01800c402") + len(data).to_bytes(8, "little") + block.to_bytes(8, "little"))
    for start in range(0, len(data), block):
        history = data[max(0, start - 32768):start]
        packer = zlib.compressobj(9, zlib.DEFLATED, -15, zdict=history) if history else zlib.compressobj(9, zlib.DEFLATED, -15)
        body = b"CK" + packer.compress(data[start:start + block]) + packer.flush()
        out += len(body).to_bytes(4, "little") + body
    return bytes(out)


class SymbolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.symbols = profiling.GameSymbols(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lookup_chain(self):
        self.assertEqual(self.symbols.resolve(0x1010, 0x1000)[:2], ("Alpha::Run", "crashlogger"))
        self.assertEqual(self.symbols.resolve(0x2010)[:2], ("Beta::Tick (guess)", "project-guess"))
        self.assertEqual(self.symbols.resolve(0x3004, None)[:2], ("Gamma::Wait", "symbols:toy-map"))
        self.assertEqual(self.symbols.resolve(0x5010, 0x5000, 77, 0x10)[:2], ("id:77+0x10", "addrlib"))
        self.assertEqual(self.symbols.resolve(0x5010, 0x5000)[:2], ("sub_5000", "pdata"))
        self.assertEqual(self.symbols.resolve(0x9000)[:2], ("game+0x9000", "none"))

    def test_research_root(self):
        self.assertEqual(profiling.research_root(config={"researchCheckout": str(self.root)}), self.root)
        self.assertIsNone(profiling.research_root(config={"researchCheckout": str(self.root / "missing")}))

    def test_module_kinds(self):
        self.assertEqual([profiling.module_kind(m) for m in MODULES], ["game", "kernel", "plugin"])
        self.assertEqual(profiling.module_kind({"name": "nvwgf2umx.dll", "path": "C:\\x\\nvwgf2umx.dll"}), "gpu-driver")
        self.assertEqual(profiling.module_kind({"name": "kernel32.dll", "path": "C:\\Windows\\System32\\kernel32.dll"}), "system")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.profile = profiling.Profile(raw_profile(RUNS), profiling.GameSymbols(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_self_total_and_waits(self):
        report = self.profile.report("main")
        self.assertEqual(report["onCpuSamples"], 7)
        self.assertEqual(report["waitingSamples"], 3)
        self_rows = {r["function"]: r["samples"] for r in report["topSelf"]}
        self.assertEqual(self_rows, {"id:77+0x10": 4, "Beta::Tick (guess)": 2, "devtools::Pump": 1})
        total = {r["function"]: r["samples"] for r in report["topTotal"]}
        self.assertEqual(total["Alpha::Run"], 7)
        self.assertEqual(total["Beta::Tick (guess)"], 6)
        self.assertEqual(report["topWaits"][0]["function"], "NtWaitForSingleObject")
        self.assertAlmostEqual(report["modules"]["game"], 85.71, places=2)
        self.assertAlmostEqual(report["modules"]["plugin"], 14.29, places=2)

    def test_wait_detected_by_leaf_name_without_flag(self):
        profile = profiling.Profile(raw_profile([[0.0, 4, 2, 0]]))
        self.assertEqual(profile.report("main")["waitingSamples"], 2)

    def test_coverage_and_credits(self):
        coverage = self.profile.coverage()
        self.assertEqual(coverage["gameSamples"], 6)
        self.assertAlmostEqual(coverage["namedPct"], 33.3)
        credits = " ".join(self.profile.credits())
        self.assertIn(profiling.CRASHLOGGER_CREDIT, credits)
        self.assertIn("SYMBOLS(toy-map)", credits)

    def test_hitches(self):
        hitch = self.profile.hitches()[0]
        self.assertEqual(hitch["samples"], 3)
        self.assertAlmostEqual(hitch["waitingPct"], 66.7)
        self.assertEqual(hitch["topSelf"][0]["function"], "id:77+0x10")
        self.assertEqual(hitch["topWaits"][0]["function"], "NtWaitForSingleObject")
        self.assertEqual(hitch["dominantStack"]["stack"], ["Alpha::Run", "Gamma::Wait", "NtWaitForSingleObject"])

    def test_folded_and_svg(self):
        folded = self.profile.folded("main")
        self.assertIn("Alpha::Run;Beta::Tick (guess);id:77+0x10 4\n", folded)
        self.assertNotIn("NtWait", folded)
        self.assertIn("NtWaitForSingleObject 3", self.profile.folded("main", waiting=True))
        svg = profiling.flamegraph_svg(folded, "toy")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Beta::Tick (guess) (6 samples", svg)

    def test_firefox_shape(self):
        from skyrim_devtools import profile_firefox

        data = self.profile.firefox().build()
        self.assertEqual(profile_firefox.check(data), [])
        self.assertEqual(data["meta"]["preprocessedProfileVersion"], 49)
        thread = data["threads"][0]
        self.assertTrue(thread["isMainThread"])
        self.assertEqual(sum(thread["samples"]["weight"]), 10)
        names = [thread["stringArray"][n] for n in thread["funcTable"]["name"]]
        self.assertIn("Alpha::Run", names)
        resources = [thread["stringArray"][n] for n in thread["resourceTable"]["name"]]
        self.assertIn("SkyrimSE.exe", resources)
        self.assertEqual([lib["name"] for lib in data["libs"]], ["SkyrimSE.exe", "ntdll.dll", "SkyrimDevTools.dll"])
        markers = [thread["markers"]["data"][i]["type"] for i in range(thread["markers"]["length"])]
        self.assertEqual(markers.count("Frame"), 2)
        self.assertEqual(markers.count("Hitch"), 1)
        idle = data["meta"]["categories"].index(next(c for c in data["meta"]["categories"] if c["name"] == "Idle"))
        wait_weight = sum(w for s, w in zip(thread["samples"]["stack"], thread["samples"]["weight"])
                          if thread["stackTable"]["category"][s] == idle)
        self.assertEqual(wait_weight, 3)
        json.dumps(data)

    def test_firefox_profile_marks(self):
        from skyrim_devtools import profile_firefox

        raw = raw_profile(RUNS)
        raw["marks"] = [[2.0, 9.0, "load", "Loading Menu"], [4.0, 4.0, "alert", "low health"], [None, 1.0, "rpc", "x"]]
        data = profiling.Profile(raw, self.profile.symbols).firefox().build()
        self.assertEqual(profile_firefox.check(data), [])
        thread = data["threads"][0]
        marks = [(thread["markers"]["data"][i], thread["markers"]["endTime"][i], thread["markers"]["phase"][i])
                 for i in range(thread["markers"]["length"]) if thread["markers"]["data"][i]["type"] == "Mark"]
        self.assertEqual([(m["kind"], m["label"]) for m, _, _ in marks], [("load", "Loading Menu"), ("alert", "low health")])
        self.assertEqual([(end, phase) for _, end, phase in marks], [(9.0, 1), (None, 0)])

    def test_firefox_counter_and_extra_markers(self):
        from skyrim_devtools import profile_firefox

        export = self.profile.firefox()
        export.add_counter("gpu_util_pct", "GPU util", [0.0, 500.0, 1000.0], [20.0, 35.0, 30.0], unit="percent")
        export.marker_schema({"name": "Mark", "display": ["marker-chart", "marker-table"], "data": []})
        export.add_markers([("load", 10.0, 40.0, {"type": "Mark"})])
        data = export.build()
        self.assertEqual(profile_firefox.check(data), [])
        counter = data["counters"][0]
        running, values = 0.0, []
        for delta in counter["samples"]["count"]:
            running += delta
            values.append(running)
        self.assertEqual(values, [0.0, 20.0, 35.0, 30.0])
        self.assertEqual(counter["display"]["graphType"], "line-accumulated")
        self.assertIn("load", data["threads"][0]["stringArray"])

    def test_firefox_check_catches_bad_tables(self):
        from skyrim_devtools import profile_firefox

        data = self.profile.firefox().build()
        data["threads"][0]["stackTable"]["prefix"][0] = 5
        self.assertTrue(profile_firefox.check(data))

    def test_firefox_serve_like_samply(self):
        import urllib.request
        from skyrim_devtools import profile_firefox

        with tempfile.TemporaryDirectory() as out:
            path = profile_firefox.write(self.profile.firefox().build(), Path(out) / "p.json.gz")
            server, url = profile_firefox.serve(path, open_browser=False)
            try:
                self.assertTrue(url.startswith("https://profiler.firefox.com/from-url/http%3A%2F%2F127.0.0.1%3A"))
                local = urllib.parse.unquote(url.split("/from-url/")[1].rstrip("/"))
                with urllib.request.urlopen(local) as response:
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                    body = gzip.decompress(response.read()) if response.headers.get("Content-Encoding") == "gzip" else response.read()
                self.assertEqual(json.loads(body)["meta"]["preprocessedProfileVersion"], 49)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(local.rsplit("/", 2)[0] + "/wrong/profile.json")
            finally:
                server.shutdown()
                server.server_close()

    def test_export_command(self):
        with tempfile.TemporaryDirectory() as out:
            with gzip.open(Path(out) / "raw.json.gz", "wt") as handle:
                json.dump(raw_profile(RUNS), handle)
            target = Path(out) / "ff.json"
            self.assertEqual(profiling.main(["export", out, "--firefox", str(target)]), 0)
            self.assertEqual(json.loads(target.read_text())["meta"]["preprocessedProfileVersion"], 49)

    def test_flamegraph_is_interactive(self):
        svg = profiling.flamegraph_svg(self.profile.folded("main"), "toy", ms_per_sample=1.0)
        self.assertIn("<script><![CDATA[", svg)
        self.assertIn('id="unzoom"', svg)
        self.assertIn('id="search"', svg)
        self.assertIn('data-ms="1"', svg)
        self.assertIn("6.0 ms", svg)

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as out:
            paths, summary = profiling.save_outputs(self.profile, out)
            self.assertTrue(Path(paths["firefox"]).exists())
            with gzip.open(paths["firefox"], "rt") as handle:
                self.assertEqual(json.load(handle)["meta"]["preprocessedProfileVersion"], 49)
            again = profiling.Profile(profiling.load_raw(out), self.profile.symbols)
            self.assertEqual(again.report("main")["onCpuSamples"], 7)
            self.assertIn("top self", (Path(out) / "report.txt").read_text())

    def test_diff(self):
        other = profiling.Profile(raw_profile([[0.0, 5, 7, 0]]), self.profile.symbols)
        result = profiling.diff(self.profile, other)
        top = result["topSelf"][0]
        self.assertEqual(top["function"], "devtools::Pump")
        self.assertAlmostEqual(top["delta"], 100.0 - 14.29, places=2)
        self.assertIn("plugin", result["modules"])
        self.assertIn("largest self changes", profiling.render_diff(result))

    def test_fetch_reads_chunks(self):
        text = json.dumps(raw_profile(RUNS))
        parts = [text[:100], text[100:]]

        async def call(method, **params):
            if method == "Profile.stop":
                return {"bytes": len(text), "chunks": 2, "summary": {}}
            return {"chunk": params["chunk"], "data": parts[params["chunk"]], "last": params["chunk"] == 1}

        raw, _ = asyncio.run(profiling.fetch(call))
        self.assertEqual(raw["hz"], 1000.0)

    def test_fetch_decodes_mszip(self):
        text = json.dumps(raw_profile(RUNS)) * 1
        packed = base64.b64encode(mszip_encode(text.encode())).decode()
        parts = [packed[:50], packed[50:]]

        async def call(method, **params):
            if method == "Profile.stop":
                self.assertEqual(params.get("encoding"), "mszip")
                return {"bytes": len(text), "encoding": "mszip", "encodedBytes": len(packed), "chunks": 2, "summary": {}}
            return {"chunk": params["chunk"], "data": parts[params["chunk"]], "last": params["chunk"] == 1}

        raw, summary = asyncio.run(profiling.fetch(call))
        self.assertEqual(raw["hz"], 1000.0)
        self.assertEqual(summary["transfer"]["encoding"], "mszip")

    def test_mszip_blocks_share_a_dictionary(self):
        data = ("".join(f"[{i % 977},{i % 13},1,0]," for i in range(40000))).encode()
        self.assertEqual(profiling.mszip_decode(mszip_encode(data)), data)
        with self.assertRaises(ValueError):
            profiling.mszip_decode(b"not mszip" * 4)

    def test_fetch_falls_back_for_an_older_plugin(self):
        text = json.dumps(raw_profile(RUNS))
        calls = []

        async def call(method, **params):
            calls.append((method, params))
            if method == "Profile.stop":
                if "encoding" in params:
                    raise RuntimeError("Unknown parameter: encoding")
                return {"bytes": len(text), "chunks": 1, "summary": {}}
            return {"chunk": 0, "data": text, "last": True}

        raw, summary = asyncio.run(profiling.fetch(call))
        self.assertEqual(raw["hz"], 1000.0)
        self.assertEqual(summary["transfer"]["encoding"], "json")
        self.assertEqual(calls[1], ("Profile.stop", {}))


class ModuleLabelTests(unittest.TestCase):
    MODULES = MODULES + [
        {"name": "amdxx64.dll", "path": "C:\\Windows\\System32\\DriverStore\\amdxx64.dll", "base": 0, "size": 0x900000,
         "game": False, "symbols": "export"},
        {"name": "d3d11.dll", "path": "C:\\Windows\\System32\\d3d11.dll", "base": 0, "size": 0x900000, "game": False,
         "symbols": "pdb"},
    ]
    FRAMES = FRAMES + [
        {"m": 3, "rva": 0x412345, "start": 0x412300, "sym": "boost::serialization::singleton", "symOff": 0x300000},  # 6 far export
        {"m": 3, "rva": 0x412400, "start": 0x4123f0, "sym": "boost::serialization::singleton", "symOff": 0x300100},  # 7
        {"m": 3, "rva": 0x1010, "start": 0x1000, "sym": "PIXBeginCapture", "symOff": 0x10},                           # 8 the export
        {"m": 4, "rva": 0x2050, "start": 0x2000, "sym": "CContext::Draw", "symOff": 0x50},                            # 9 pdb
        {"m": 3, "rva": 0x777, "sym": "CompatString", "symOff": 0x7000},                                             # 10 no pdata
    ]
    STACKS = STACKS + [
        [0, 9],   # 6 Alpha > d3d11 Draw
        [6, 8],   # 7 > amd PIX export
        [7, 6],   # 8 > amd far
        [8, 7],   # 9 > amd far
        [6, 10],  # 10 Alpha > Draw > amd no pdata (single)
    ]

    def profile(self, runs):
        raw = raw_profile(runs)
        raw["modules"], raw["frames"], raw["stacks"] = self.MODULES, self.FRAMES, self.STACKS
        return profiling.Profile(raw)

    def test_labels(self):
        profile = self.profile([[0.0, 9, 5, 0], [5.0, 10, 2, 0]])
        labels = [profile.functions[f][0] for f in profile.frame_function[6:]]
        self.assertEqual(labels, ["amdxx64.dll+0x412300", "amdxx64.dll+0x4123f0", "amdxx64.dll!PIXBeginCapture",
                                  "CContext::Draw", "amdxx64.dll+0x777"])
        self.assertNotIn("boost", json.dumps(profile.summary()))

    def test_runs_collapse(self):
        profile = self.profile([[0.0, 9, 5, 0], [5.0, 10, 2, 0]])
        names = [profile.functions[f][0] for f in reversed(profile.stack_functions(9))]
        self.assertEqual(names, ["sub_1000", "CContext::Draw", "amdxx64.dll!PIXBeginCapture", "[amdxx64.dll]"])
        single = [profile.functions[f][0] for f in reversed(profile.stack_functions(10))]
        self.assertEqual(single[-1], "amdxx64.dll+0x777")
        report = profile.report("main")
        self.assertEqual(report["topSelf"][0]["function"], "[amdxx64.dll]")
        self.assertIn("[amdxx64.dll] 5", profile.folded("main"))
        data = profile.firefox().build()
        names = [data["threads"][0]["stringArray"][n] for n in data["threads"][0]["funcTable"]["name"]]
        self.assertIn("[amdxx64.dll]", names)
        self.assertEqual(len(profile.function_meta), len(profile.functions))

    def test_overhead_reports_throttling_and_other_threads(self):
        raw = raw_profile(RUNS)
        raw.update({"ticks": 1000, "suspendBudgetPct": 2.5, "otherEvery": 4})
        raw["threads"][0]["throttled"] = 120
        raw["threads"].append({"tid": 2, "name": "worker", "main": False, "samples": 3, "walked": 3, "suspendedMs": 5.0,
                               "throttled": 0, "runs": [[0.0, 0, 4, 0]]})
        overhead = profiling.Profile(raw).overhead()
        self.assertEqual(overhead["mainThrottledPct"], 12.0)
        self.assertEqual(overhead["otherHz"], 250.0)
        self.assertEqual(overhead["otherMaxSuspendedPct"], 0.5)
        text = profiling.render_text(profiling.Profile(raw).summary())
        self.assertIn("held the previous stack", text)
        self.assertIn("1 other threads at 250.0 Hz", text)



class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.symbols = profiling.GameSymbols(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_top_snapshot_folds_pcs_into_functions(self):
        reply = {"seconds": 3.0, "hz": 250.0, "ready": True, "warm": True, "samples": 100, "waiting": 20,
                 "self": [dict(FRAMES[0], n=50), dict(FRAMES[0], rva=0x1020, n=10), dict(FRAMES[3], n=15), dict(FRAMES[5], n=5)],
                 "total": [dict(FRAMES[0], n=80), dict(FRAMES[3], n=15)],
                 "waits": [dict(FRAMES[4], n=20)], "modules": MODULES,
                 "memory": {"workingSetMb": 3000.0, "privateMb": 3500.0, "residentPct": 85.7, "pageFaultsPerS": 12.0,
                            "systemAvailableMb": 9000.0},
                 "threads": {"count": 80, "processCpuPct": 140.0, "cores": 16,
                             "top": [{"tid": 1, "name": "main", "cpuS": 10.0, "pct": 90.0}]},
                 "gpu": {"utilPct": 60.0, "engines": {"3D": 60.0}, "vramMb": 2048.0}, "profiling": False}

        async def call(method, **params):
            if method == "Profile.top":
                return reply
            return {"fps": 60.0, "p50Ms": 16.6, "p99Ms": 20.1, "maxMs": 25.0, "count": 180}

        snap = asyncio.run(profile_views.top_snapshot(call, self.symbols))
        self.assertEqual(snap["topSelf"][0]["function"], "Alpha::Run")
        self.assertEqual(snap["topSelf"][0]["samples"], 60)
        self.assertEqual(snap["topSelf"][0]["pct"], 60.0)
        self.assertEqual(snap["topSelf"][0]["totalPct"], 80.0)
        self.assertEqual(snap["main"]["busyPct"], 80.0)
        self.assertEqual(snap["waits"][0]["function"], "NtWaitForSingleObject")
        self.assertAlmostEqual(snap["main"]["namedPct"], 80.0)
        text = profile_views.render_top(snap)
        self.assertIn("fps", text)
        self.assertIn("Alpha::Run", text)
        self.assertIn("VRAM 2.00 GB", text)
        self.assertTrue(any(profiling.CRASHLOGGER_CREDIT in c for c in snap["credits"]))

    def test_top_snapshot_cold(self):
        async def call(method, **params):
            if method == "Profile.top":
                return {"seconds": 3.0, "hz": 250.0, "ready": False, "warm": False}
            raise RuntimeError("no Present hook")

        snap = asyncio.run(profile_views.top_snapshot(call))
        self.assertIn("no main-thread samples", profile_views.render_top(snap))

    def test_recorder_marks_window(self):
        path = self.root / "t.jsonl"
        lines = [{"t": "tool", "name": "walk", "startMs": 10_500, "endMs": 11_000},
                 {"t": "narrate", "ms": 12_000, "text": "entering Riverwood"},
                 {"t": "tool", "name": "late", "startMs": 99_000, "endMs": 99_100},
                 {"t": "status", "ms": 10_600}]
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
        marks = profile_views.recorder_marks(path, 10_000, 5_000)
        self.assertEqual(marks, [[500, 1000, "tool", "walk"], [2000, 2000, "narrate", "entering Riverwood"]])

    def test_timeline_html(self):
        raw = raw_profile(RUNS)
        raw["marks"] = [[2.0, 9.0, "load", "Loading Menu"], [1.0, 1.5, "rpc", "Console.execute</script>"]]
        profile = profiling.Profile(raw, self.symbols)
        data = profile_views.timeline_data(profile)
        self.assertEqual([m[2] for m in data["marks"]], ["rpc", "load", "hitch"])
        thread = data["threads"][0]
        self.assertEqual(sum(s[2] for s in thread["samples"]), 10)
        self.assertEqual(thread["onCpu"], 7)
        leaf = thread["samples"][0][1]
        names = []
        while leaf >= 0:
            names.append(data["functions"][data["tree"][leaf][1]][0])
            leaf = data["tree"][leaf][0]
        self.assertEqual(names[::-1], ["Alpha::Run", "Beta::Tick (guess)", "id:77+0x10"])
        page = profile_views.timeline_html(profile)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("</script>\"", page)
        self.assertEqual(page.count("</script>"), 1)
        with tempfile.TemporaryDirectory() as out:
            paths, _ = profiling.save_outputs(profile, out)
            self.assertTrue(Path(paths["timeline"]).exists())


if __name__ == "__main__":
    unittest.main()


def gpu_raw():
    raw = raw_profile(RUNS)
    # GPU clock reads 1000 s less than the CPU clock; the tightest issue bound puts frame 1 at 0-15 ms. The CPU
    # issued frame 1 in 5 ms and the GPU took 15, so the GPU fell behind (GPU-bound); frame 2 only followed it.
    raw["gpu"] = {"available": True, "reason": "", "backbuffer": [1920, 1080],
                  "labels": ["frame", "present", "dsv 2048x2048 array", "rtv 1920x1080", "rtv 960x540"],
                  "disjointFrames": 0, "staleFrames": 0,
                  "frames": [[1, 0.0, 5.0, 4.0, 0, [[0, -999.9995], [2, -999.998], [3, -999.994], [4, -999.990], [1, -999.9845]]],
                             [2, 16.0, 149.0, 4.0, 0, [[0, -999.8508], [3, -999.8506], [1, -999.8505]]]]}
    raw["counters"] = [[0.0, 100, 2 << 30, 0, 0, 0, 8 << 30], [250.0, 150, 2 << 30, 1 << 20, 0, 1_250_000, 8 << 30]]
    raw["gpuCounters"] = [[500.0, 70.0, 2048.0]]
    raw["marks"] = [[2.0, 9.0, "load", "Loading Menu"]]
    return raw


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.profile = profiling.Profile(gpu_raw(), profiling.GameSymbols(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_alignment_and_passes(self):
        from skyrim_devtools.profile_gpu import gpu_frames, pass_kind

        frames, offset = gpu_frames(self.profile.raw)
        self.assertAlmostEqual(offset, 999999.5, places=3)
        self.assertAlmostEqual(frames[0]["gpuStart"], 0.0, places=3)
        self.assertAlmostEqual(frames[0]["gpuMs"], 15.0, places=3)
        self.assertAlmostEqual(frames[1]["gpuStart"], 148.7, places=3)
        kinds = [p[1] for p in frames[0]["passes"]]
        self.assertEqual(kinds, ["frame", "shadows", "scene", "post"])
        self.assertEqual(pass_kind("dsv 1920x1080", [1920, 1080]), "main")
        self.assertEqual(pass_kind("rtv 512x512", [1920, 1080]), "post")

    def test_summary_and_frames(self):
        from skyrim_devtools.profile_gpu import frame_table

        rows = frame_table(self.profile)
        self.assertEqual(rows[0]["cpuMs"], 7.0)   # on-CPU samples at 0-5 ms and 9 ms of frame 1 (0-16 ms)
        self.assertEqual(rows[0]["bound"], "gpu")
        self.assertTrue(rows[1]["hitch"])
        self.assertEqual(rows[1]["bound"], "capped")
        self.assertEqual(rows[0]["excessMs"], 10.0)
        self.assertFalse(rows[0]["paced"])
        self.assertEqual(rows[0]["gpuBusyMs"], 15.0)
        self.assertTrue(rows[1]["paced"])
        self.assertEqual(rows[1]["utilPct"], 70.0)
        gpu = self.profile.summary()["gpu"]
        self.assertEqual(gpu["frames"], 2)
        self.assertEqual(gpu["timedPct"], 100.0)
        self.assertEqual(gpu["passes"][0]["kind"], "post")
        text = profiling.render_text(self.profile.summary())
        self.assertIn("== GPU: 2 of 2 frames timed", text)

    def test_no_gpu_reason(self):
        raw = raw_profile(RUNS)
        raw["gpu"] = {"available": False, "reason": "the device refused timestamp queries", "frames": []}
        summary = profiling.Profile(raw, profiling.GameSymbols(self.root)).summary()
        self.assertEqual(summary["gpu"]["frames"], 0)
        self.assertIn("refused", profiling.render_text(summary))

    def test_sqlite_export_and_queries(self):
        from skyrim_devtools.profile_sqlite import export_sqlite, query

        path = self.root / "out" / "profile.sqlite"
        counts = export_sqlite(self.profile, path)
        self.assertEqual(counts["Frames"], 2)
        self.assertEqual(counts["CpuSamples"], 4)
        top = query(path, "SELECT name, SUM(weight) AS n FROM LeafSamplesV WHERE state = 'running' "
                          "GROUP BY name ORDER BY n DESC")
        self.assertEqual(top["rows"][0], ["id:77+0x10", 4])
        during_hitch = query(path, "SELECT s.name, SUM(c.weight) FROM CpuSamples c JOIN Frames f ON f.hitch = 1 "
                                   "AND c.timestamp >= f.start AND c.timestamp < f.end JOIN Callchains cc ON cc.id = c.callchainId "
                                   "AND cc.depth = 0 JOIN SymbolsV s ON s.id = cc.symbolId GROUP BY s.id")
        self.assertEqual(during_hitch["rows"], [])  # the toy samples all fall in frame 1
        gpu_first = query(path, "SELECT frame FROM Frames WHERE gpu_ms > cpu_ms")
        self.assertEqual(gpu_first["rows"], [[1], [2]])
        loads = query(path, "SELECT s.name, SUM(c.weight) AS n FROM MarkersV m JOIN CpuSamples c ON c.timestamp >= m.start "
                            "AND c.timestamp < m.end JOIN Callchains cc ON cc.id = c.callchainId AND cc.depth = 0 "
                            "JOIN SymbolsV s ON s.id = cc.symbolId WHERE m.kind = 'load' AND c.state = 'running' GROUP BY s.id ORDER BY n DESC")
        self.assertEqual(loads["rows"][0][0], "Beta::Tick (guess)")
        counters = query(path, "SELECT name, value FROM CountersV WHERE name IN ('page_faults_per_s', 'gpu_util_pct') ORDER BY name")
        self.assertEqual(counters["rows"], [["gpu_util_pct", 70.0], ["page_faults_per_s", 200.0]])
        meta = dict(query(path, "SELECT key, value FROM Meta")["rows"])
        self.assertIn(profiling.CRASHLOGGER_CREDIT, meta["credits"])
        passes = query(path, "SELECT kind, COUNT(*) FROM GpuRangesV GROUP BY kind ORDER BY kind")
        self.assertIn(["shadows", 1], passes["rows"])
        limited = query(path, "SELECT * FROM CpuSamples", limit=2)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["rows"]), 2)

    def test_documented_queries_run(self):
        import re

        from skyrim_devtools.profile_sqlite import export_sqlite, query

        path = self.root / "doc.sqlite"
        export_sqlite(self.profile, path)
        doc = (Path(__file__).resolve().parent.parent / "docs" / "profiling.md").read_text()
        blocks = re.findall(r"```sql\n(.*?)```", doc, re.S)
        self.assertEqual(len(blocks), 3)
        results = [query(path, sql.strip().rstrip(";")) for sql in blocks]
        self.assertEqual(results[0]["rows"], [])
        self.assertEqual([r[0] for r in results[1]["rows"]], [1])
        self.assertEqual(results[2]["rows"][0][0], "Beta::Tick (guess)")

    def test_query_is_read_only(self):
        import sqlite3

        from skyrim_devtools.profile_sqlite import export_sqlite, query

        path = self.root / "p.sqlite"
        export_sqlite(self.profile, path)
        for sql in ("DELETE FROM Frames", "PRAGMA query_only = OFF", f"ATTACH DATABASE '{self.root / 'x.db'}' AS x",
                    "CREATE TABLE t (a)", "SELECT 1; DELETE FROM Frames"):
            with self.assertRaises((sqlite3.DatabaseError, sqlite3.Warning, sqlite3.ProgrammingError), msg=sql):
                query(path, sql)
        self.assertFalse((self.root / "x.db").exists())
        self.assertEqual(query(path, "SELECT COUNT(*) FROM Frames")["rows"], [[2]])

    def test_timeline_has_gpu_rows(self):
        data = profile_views.timeline_data(self.profile)
        self.assertEqual(len(data["gpu"]["frames"]), 2)
        self.assertIn(["rtv 1920x1080", "scene"], data["gpu"]["passes"])
        html_text = profile_views.timeline_html(self.profile)
        self.assertIn("GPU passes", html_text)

    def test_save_outputs_writes_sqlite(self):
        paths, _ = profiling.save_outputs(self.profile, self.root / "saved")
        self.assertTrue(Path(paths["sqlite"]).is_file())

    def test_classify_vsync_capped(self):
        from skyrim_devtools.profile_gpu import classify

        # A 60 fps capped frame: the GPU span fills the frame only because it followed a sleeping CPU.
        self.assertEqual(classify(16.7, 5.0, 16.2, excess_ms=-0.2), "capped")
        self.assertEqual(classify(16.7, 5.0, 16.2, excess_ms=None, util_pct=24.0), "capped")
        self.assertEqual(classify(16.7, 5.0, 16.2, excess_ms=11.0, util_pct=24.0), "capped")
        self.assertEqual(classify(16.7, 5.0, 16.2, excess_ms=11.0), "gpu")
        self.assertEqual(classify(16.7, 5.0, 16.2, excess_ms=-0.2, util_pct=97.0), "gpu")
        self.assertEqual(classify(16.7, 16.0, 16.2, excess_ms=-0.2), "cpu")
        self.assertEqual(classify(33.0, 31.0, 30.0, excess_ms=12.0), "cpu")
        self.assertIsNone(classify(16.7, 5.0, None))

    def test_firefox_gpu_tracks(self):
        from skyrim_devtools import profile_firefox

        data = profiling.profile_firefox_data(self.profile)
        self.assertEqual(profile_firefox.check(data), [])
        self.assertIn("GpuFrame", {s["name"] for s in data["meta"]["markerSchema"]})
        main = data["threads"][0]
        kinds = [d.get("type") for d in main["markers"]["data"] if d]
        self.assertEqual(kinds.count("GpuFrame"), 2)
        self.assertIn("GpuPass", kinds)
        bounds = [d["bound"] for d in main["markers"]["data"] if d and d.get("type") == "GpuFrame"]
        self.assertEqual(bounds, ["gpu", "capped (vsync or frame limiter)"])
        self.assertEqual([c["name"] for c in data["counters"]], ["gpu_frame_ms", "gpu_util_pct"])

    def test_report_names_capped(self):
        text = profiling.render_text(self.profile.summary())
        self.assertIn("capped (vsync or frame limiter) 50.0%", text)
        self.assertIn("kept up with the CPU's submissions in 50.0% of frames", text)

    def test_top_gpu_timing(self):
        timing = profile_views._gpu_timing({"frames": 50, "gpuMsP50": 15.5, "gpuMsP95": 16.0, "passes": []}, 16.6, 0.5)
        self.assertEqual(timing["cpuMsP50"], 8.3)
        self.assertEqual(timing["bound"], "gpu")
        capped = profile_views._gpu_timing({"frames": 50, "gpuMsP50": 16.2, "passes": []}, 16.7, 0.3, util_pct=24.0)
        self.assertEqual(capped["bound"], "capped")


def amd_raw():
    raw = gpu_raw()
    raw["gpuMetrics"] = {"source": "adlx", "reason": None, "gpu": "Toy Radeon", "sampleUs": 900,
                         "names": ["gpu_util_pct", "gpu_clock_mhz", "gpu_vram_mb", "gpu_fan_rpm"],
                         "supported": ["gpu_util_pct", "gpu_clock_mhz", "gpu_vram_mb"],
                         "rows": [[0.0, 90.0, 2500, 6000, None], [1000.0, 80.0, 2400, 6100, None]]}
    raw["gpaCounters"] = {
        "ok": True, "device": "Toy Radeon", "passCount": 3, "framesUsed": 4, "sampleCount": 2,
        "samples": ["clear 1", "clear 2"], "missingCounters": ["VALUUtilization"],
        "overhead": {"initMs": 40.0, "cpuUsPerFrame": 120.0, "baselineFps": 60.0, "captureFps": 30.0},
        "counters": [
            {"name": "GPUTime", "group": "Timing", "usage": "milliseconds", "values": [3.0, 1.0]},
            {"name": "GPUBusy", "group": "Timing", "usage": "percentage", "values": [100.0, 60.0]},
            {"name": "FetchSize", "group": "GlobalMemory", "usage": "kilobytes", "values": [10.0, None]},
            {"name": "TexAveAnisotropy", "group": "TextureUnit", "usage": "items", "values": [None, None]},
        ]}
    return raw


class AmdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_symbols(self.root)
        self.profile = profiling.Profile(amd_raw(), profiling.GameSymbols(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_board_metrics_replace_pdh(self):
        from skyrim_devtools.profile_gpu import counter_series

        series = counter_series(self.profile.raw)
        util = [(at, v) for at, name, v in series if name == "gpu_util_pct"]
        self.assertEqual(util, [(0.0, 90.0), (1000.0, 80.0)])  # PDH's 70 % is dropped
        self.assertIn((500.0, "gpu_process_vram_mb", 2048.0), series)
        self.assertFalse(any(name == "gpu_fan_rpm" for _, name, _ in series))
        board = self.profile.summary()["gpu"]["board"]
        self.assertEqual(board["source"], "adlx")
        self.assertEqual(board["metrics"]["gpu_clock_mhz"], {"mean": 2450.0, "max": 2500})
        text = profiling.render_text(self.profile.summary())
        self.assertIn("== GPU board (adlx, Toy Radeon", text)

    def test_pdh_only_without_board(self):
        from skyrim_devtools.profile_gpu import counter_series

        raw = gpu_raw()
        raw["gpuMetrics"] = {"source": None, "reason": "no AMD driver"}
        series = counter_series(raw)
        self.assertIn((500.0, "gpu_util_pct", 70.0), series)
        self.assertIn((500.0, "gpu_vram_mb", 2048.0), series)
        summary = profiling.Profile(raw, profiling.GameSymbols(self.root)).summary()
        self.assertEqual(summary["gpu"]["board"], {"source": None, "reason": "no AMD driver"})

    def test_gpa_summary(self):
        from skyrim_devtools.profile_gpa import render, summarize

        summary = summarize(self.profile.raw["gpaCounters"])
        rows = {r["name"]: r for r in summary["counters"]}
        self.assertEqual(rows["GPUTime"]["value"], 4.0)
        self.assertEqual(rows["GPUBusy"]["value"], 90.0)  # weighted by GPU time: (100*3 + 60*1) / 4
        self.assertEqual(rows["FetchSize"]["value"], 10.0)
        self.assertIsNone(rows["TexAveAnisotropy"]["value"])
        self.assertEqual(summary["heaviestSamples"][0]["label"], "clear 1")
        text = "\n".join(render(summary))
        self.assertIn("3 passes over 4 frames", text)
        self.assertIn("not offered on this GPU: VALUUtilization", text)
        failed = summarize({"ok": False, "error": "no device"})
        self.assertIn("failed: no device", render(failed)[0])

    def test_gpa_in_sqlite_and_outputs(self):
        from skyrim_devtools.profile_sqlite import export_sqlite, query

        path = self.root / "amd.sqlite"
        counts = export_sqlite(self.profile, path)
        self.assertEqual(counts["GpaCounters"], 5)
        busy = query(path, "SELECT label, value FROM GpaCountersV WHERE name = 'GPUBusy' ORDER BY sample")
        self.assertEqual(busy["rows"], [["clear 1", 100.0], ["clear 2", 60.0]])
        meta = dict(query(path, "SELECT key, value FROM Meta")["rows"])
        self.assertEqual(meta["gpuBoardSource"], "adlx")
        self.assertEqual(meta["gpaPassCount"], "3")
        paths, report = profiling.save_outputs(self.profile, self.root / "saved")
        self.assertTrue(Path(paths["gpuCounters"]).is_file())
        self.assertTrue(report["gpuCounters"]["ok"])

    def test_board_tracks_in_firefox_export(self):
        from skyrim_devtools import profile_firefox

        data = profiling.profile_firefox_data(self.profile)
        self.assertEqual(profile_firefox.check(data), [])
        tracks = {c["name"]: c for c in data["counters"]}
        for name in ("gpu_util_pct", "gpu_clock_mhz", "gpu_vram_mb"):
            self.assertIn(name, tracks)
        self.assertNotIn("gpu_fan_rpm", tracks)  # never reported, so no track
        clock = tracks["gpu_clock_mhz"]
        self.assertEqual(clock["display"]["unit"], "MHz")
        running, levels = 0.0, []
        for delta in clock["samples"]["count"]:
            running += delta
            levels.append(running)
        self.assertEqual(levels, [0.0, 2500.0, 2400.0])
        self.assertIn("adlx", clock["description"])

    def test_board_tracks_not_duplicated(self):
        from skyrim_devtools.profile_gpu import firefox_board_tracks

        export = self.profile.firefox()
        export.add_counter("gpu_util_pct", "GPU utilisation", [0.0], [50.0], unit="percent")
        added = firefox_board_tracks(export, self.profile.raw)
        self.assertNotIn("gpu_util_pct", added)
        self.assertEqual([c["name"] for c in export.counters].count("gpu_util_pct"), 1)
        raw = gpu_raw()
        raw["gpuMetrics"] = {"source": None, "reason": "no AMD driver"}
        self.assertEqual(firefox_board_tracks(profiling.Profile(raw, profiling.GameSymbols(self.root)).firefox(), raw), [])

    def test_gpa_skipped_counters_are_warnings(self):
        from skyrim_devtools.profile_gpa import summarize

        result = dict(self.profile.raw["gpaCounters"], warnings=["counter VALUUtilization is not offered by this device; skipped"])
        summary = summarize(result)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["warnings"], result["warnings"])

    def test_timeline_counter_rows(self):
        data = profile_views.timeline_data(self.profile)
        names = [c[0] for c in data["counters"]]
        self.assertEqual(names[:3], ["gpu_util_pct", "gpu_clock_mhz", "gpu_vram_mb"])
        self.assertIn("gpu_process_vram_mb", names)
        self.assertIn("drawCounter", profile_views.timeline_html(self.profile))

    def test_top_board_line(self):
        board = {"source": "adlx", "gpu": "Toy", "metrics": {"gpu_util_pct": 97, "gpu_clock_mhz": 2900,
                                                            "gpu_board_power_w": 250, "gpu_vram_mb": 6000}}
        reply = {"seconds": 3.0, "hz": 250.0, "ready": True, "warm": True, "samples": 10, "waiting": 0,
                 "self": [dict(FRAMES[0], n=10)], "total": [dict(FRAMES[0], n=10)], "waits": [], "modules": MODULES,
                 "gpu": {"utilPct": None, "engines": {}, "vramMb": None, "board": board}, "profiling": True}

        async def call(method, **params):
            return reply if method == "Profile.top" else {"fps": 60.0}

        text = profile_views.render_top(asyncio.run(profile_views.top_snapshot(call, profiling.GameSymbols(self.root))))
        self.assertIn("GPU board  97 %  core 2900 MHz", text)
        self.assertIn("power 250 W", text)
        self.assertIn("VRAM used 5.86 GB", text)
