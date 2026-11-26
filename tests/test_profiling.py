import asyncio
import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import zlib

from skyrim_devtools import profiling


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

    def test_gecko_shape(self):
        gecko = self.profile.gecko()
        thread = gecko["threads"][0]
        self.assertEqual(len(thread["samples"]["data"]), 10)
        strings = thread["stringTable"]
        self.assertIn("Alpha::Run (in SkyrimSE.exe)", strings)
        for prefix, frame in thread["stackTable"]["data"]:
            self.assertTrue(prefix is None or prefix < len(thread["stackTable"]["data"]))
            self.assertLess(frame, len(thread["frameTable"]["data"]))
        self.assertEqual(len(thread["markers"]["data"]), 2)
        json.dumps(gecko)

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as out:
            paths, summary = profiling.save_outputs(self.profile, out)
            self.assertTrue(Path(paths["firefox"]).exists())
            with gzip.open(paths["firefox"], "rt") as handle:
                self.assertEqual(json.load(handle)["meta"]["version"], 27)
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


if __name__ == "__main__":
    unittest.main()
