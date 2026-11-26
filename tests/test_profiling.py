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
