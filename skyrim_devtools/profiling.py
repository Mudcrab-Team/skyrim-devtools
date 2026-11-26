"""Sampling CPU profiles of the game: symbolisation, reports, flame graphs and Firefox Profiler output.

The plugin's Profile.* methods sample thread stacks and return raw addresses with module, RVA, unwind
function start and Address Library ID. Names for game code come from local symbol tables that are never
part of this package (see docs/profiling.md, Symbols): a skyrim-research checkout's CrashLogger PDB tables
and project names. Everything written here goes to a local output directory.
"""

import argparse
import asyncio
import bisect
from collections import Counter, defaultdict
import gzip
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import zlib

CRASHLOGGER_CREDIT = "CRASHLOGGER_PDB(59818@2026.10.24)"
FLAG_WAITING = 1
WAIT_RE = re.compile(
    r"^(Nt|Zw)(WaitFor|DelayExecution|RemoveIoCompletion|SignalAndWait|UserMsgWaitFor|AlertThreadByThreadId|"
    r"WaitForAlertByThreadId|ReplyWaitReceivePort|AlpcSendWaitReceivePort|UserGetMessage|GdiDdDDIWaitFor|"
    r"YieldExecution|ReadFile|DeviceIoControlFile|FsControlFile|QueryVolumeInformationFile)")
SYSTEM_DIR_RE = re.compile(r"\\windows\\(system32|syswow64|winsxs)\\", re.I)
GPU_DRIVER_RE = re.compile(r"^(nvwgf2umx|nvldumdx|nvd3dumx|nvoglv64|amdxx64|atidxx64|amdxc64|igd10iumd64|igdumdim64|igc64|"
                           r"d3d11|d3d12|dxgi|d3d10warp|nvapi64|nvgpucomp64|igdgmm64)", re.I)
PLUGIN_NAME = "skyrimdevtools.dll"
CHUNK_TIMEOUT = 60


# ---- symbols ------------------------------------------------------------------------------------


def research_root(explicit=None, config=None):
    """The local skyrim-research checkout that holds the symbol tables, or None."""
    for candidate in (explicit, os.environ.get("SKYRIM_DEVTOOLS_RESEARCH"), (config or {}).get("researchCheckout")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
    return None


class GameSymbols:
    """Names for game RVAs, best source first: CrashLogger PDB names, project names, sibling symbol
    alignment. Function starts come from the image's unwind table (sent by the plugin) and the PDB
    boundary table."""

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.names = {}
        self.project = {}
        self.aligned = {}
        self.bound_starts = []
        self.bound_sizes = []
        self.loaded = []
        if self.root:
            self._load()

    def _load(self):
        pdb = self.root / "reconstruction" / "generated" / "crashlogger_pdb"
        for path in sorted((pdb / "names").glob("*.tsv")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    rva, _, name = line.partition("\t")
                    self.names[int(rva, 16)] = name
        bounds = []
        for path in sorted((pdb / "boundaries").glob("*.tsv")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    start, _, size = line.partition("\t")
                    bounds.append((int(start, 16), int(size, 16)))
        bounds.sort()
        self.bound_starts = [b[0] for b in bounds]
        self.bound_sizes = [b[1] for b in bounds]
        if self.names or bounds:
            self.loaded.append(f"crashlogger names {len(self.names)}, boundaries {len(bounds)}")
        progress = self.root / "data" / "progress.json"
        if progress.exists():
            for item in json.loads(progress.read_text(encoding="utf-8")).get("functions", []):
                if item.get("name") and isinstance(item.get("rva"), int):
                    self.project[item["rva"]] = (item["name"], item.get("name_origin") or "GUESS")
            self.loaded.append(f"project names {len(self.project)}")
        alignment = self.root / "data" / "symbol-alignment.json"
        if alignment.exists():
            for rva, item in json.loads(alignment.read_text(encoding="utf-8")).get("functions", {}).items():
                if item.get("name"):
                    self.aligned[int(rva, 16)] = (item["name"], item.get("source", "symbols"))
            self.loaded.append(f"aligned symbols {len(self.aligned)}")

    def containing(self, rva):
        index = bisect.bisect_right(self.bound_starts, rva) - 1
        if index >= 0 and rva < self.bound_starts[index] + max(self.bound_sizes[index], 1):
            return self.bound_starts[index]
        return None

    def name_at(self, start):
        if start in self.names:
            return self.names[start], "crashlogger"
        if start in self.project:
            name, origin = self.project[start]
            if origin == "GUESS":
                return f"{name} (guess)", "project-guess"
            return name, "project"
        if start in self.aligned:
            name, source = self.aligned[start]
            return name, f"symbols:{source}"
        return None

    def resolve(self, rva, start=None, ident=None, ident_offset=None):
        """(label, source, function start) for a game RVA."""
        starts = [s for s in (start, self.containing(rva)) if s is not None]
        for candidate in starts:
            found = self.name_at(candidate)
            if found:
                return found[0], found[1], candidate
        function = starts[0] if starts else rva
        if ident is not None and ident_offset is not None and ident_offset < 0x4000:
            label = f"id:{ident}" + (f"+0x{ident_offset:x}" if ident_offset else "")
            return label, "addrlib", function
        if starts:
            return f"sub_{function:x}", "pdata", function
        return f"game+0x{rva:x}", "none", rva


def module_kind(module):
    name = module.get("name", "").lower()
    if module.get("game"):
        return "game"
    if name == PLUGIN_NAME:
        return "plugin"
    if name.startswith("skse64"):
        return "skse"
    if GPU_DRIVER_RE.match(name):
        return "gpu-driver"
    if name in {"ntdll.dll", "win32u.dll"}:
        return "kernel"
    if SYSTEM_DIR_RE.search(module.get("path", "")):
        return "system"
    return "other-dll"


# ---- the profile model --------------------------------------------------------------------------


class Profile:
    """A symbolised profile. `raw` is what Profile.stop returned (plus optional `context`)."""

    def __init__(self, raw, symbols=None):
        self.raw = raw
        self.symbols = symbols or GameSymbols()
        self.modules = raw.get("modules", [])
        self.kinds = [module_kind(m) for m in self.modules]
        self.sources = Counter()
        self.functions = []      # (label, module name, kind)
        self.function_index = {}
        self.frame_function = [self._function(f) for f in raw.get("frames", [])]
        self.stacks = raw.get("stacks", [])
        self._stack_cache = {}

    def _function(self, frame, count_source=True):
        if "m" not in frame:
            key = (f"0x{frame.get('addr', 0):x}", "?", "unknown")
            source = "none"
        else:
            module = self.modules[frame["m"]]
            kind = self.kinds[frame["m"]]
            if kind == "game":
                label, source, _ = self.symbols.resolve(frame["rva"], frame.get("start"), frame.get("id"), frame.get("idOff"))
            elif frame.get("sym"):
                label, source = frame["sym"], "dbghelp"
            elif frame.get("start") is not None:
                label, source = f"sub_{frame['start']:x}", "pdata"
            else:
                label, source = f"+0x{frame['rva']:x}", "none"
            key = (label, module["name"], kind)
        if count_source:
            self.sources[source] += 1
        if key not in self.function_index:
            self.function_index[key] = len(self.functions)
            self.functions.append(key)
        return self.function_index[key]

    def stack_functions(self, stack):
        """Function indices of a stack, leaf first."""
        cached = self._stack_cache.get(stack)
        if cached is None:
            out = []
            index = stack
            while index is not None and index >= 0:
                prefix, frame = self.stacks[index]
                out.append(self.frame_function[frame])
                index = prefix
            cached = self._stack_cache[stack] = tuple(out)
        return cached

    def is_wait(self, stack, flags):
        if flags & FLAG_WAITING:
            return True
        functions = self.stack_functions(stack)
        return bool(functions) and bool(WAIT_RE.match(self.functions[functions[0]][0]))

    def threads(self, which="all"):
        for thread in self.raw.get("threads", []):
            if which == "all" or (which == "main" and thread.get("main")) or str(thread.get("tid")) == str(which):
                yield thread

    def samples(self, which="all"):
        """(thread, time ms, stack, weight, waiting) for every run."""
        period = 1000.0 / self.raw.get("hz", 1000.0)
        for thread in self.threads(which):
            for at, stack, count, flags in thread.get("runs", []):
                yield thread, at, stack, count, self.is_wait(stack, flags), period

    def coverage(self):
        """Share of game-module samples (on-CPU, all threads) whose leaf function carries a real name."""
        named = total = 0
        by_source = Counter()
        for thread, _, stack, weight, waiting, _ in self.samples():
            if waiting:
                continue
            functions = self.stack_functions(stack)
            if not functions:
                continue
            label, _, kind = self.functions[functions[0]]
            if kind != "game":
                continue
            total += weight
            source = ("named" if not label.startswith(("sub_", "id:", "game+0x")) else
                      "addrlib" if label.startswith("id:") else "unnamed")
            by_source[source] += weight
            named += weight if source == "named" else 0
        return {"gameSamples": total, "namedPct": round(100.0 * named / total, 1) if total else None,
                "split": {k: round(100.0 * v / total, 1) for k, v in by_source.items()} if total else {}}

    def report(self, which="all", top=20, stacks=8):
        self_on, total_on, self_wait = Counter(), Counter(), Counter()
        modules = Counter()
        stack_weight = Counter()
        wait_stack_weight = Counter()
        on = off = 0
        for thread, _, stack, weight, waiting, _ in self.samples(which):
            functions = self.stack_functions(stack)
            if not functions:
                continue
            if waiting:
                off += weight
                self_wait[functions[0]] += weight
                wait_stack_weight[stack] += weight
                continue
            on += weight
            self_on[functions[0]] += weight
            for f in set(functions):
                total_on[f] += weight
            modules[self.functions[functions[0]][2]] += weight
            stack_weight[stack] += weight

        def row(index, count, base):
            label, module, kind = self.functions[index]
            return {"function": label, "module": module, "kind": kind, "samples": count,
                    "pct": round(100.0 * count / base, 2) if base else 0.0}

        def stack_rows(weights, base):
            rows = []
            for stack, count in weights.most_common(stacks):
                names = [self.functions[f][0] for f in reversed(self.stack_functions(stack))]
                rows.append({"samples": count, "pct": round(100.0 * count / base, 2) if base else 0.0,
                             "stack": names[-24:]})
            return rows

        period = 1000.0 / self.raw.get("hz", 1000.0)
        result = {
            "threads": which, "hz": self.raw.get("hz"), "durationS": round(self.raw.get("durationS", 0.0), 2),
            "onCpuSamples": on, "waitingSamples": off,
            "onCpuMs": round(on * period, 1), "waitingMs": round(off * period, 1),
            "topSelf": [row(i, c, on) for i, c in self_on.most_common(top)],
            "topTotal": [row(i, c, on) for i, c in total_on.most_common(top)],
            "hotStacks": stack_rows(stack_weight, on),
            "modules": {k: round(100.0 * v / on, 2) for k, v in modules.most_common()} if on else {},
            "topWaits": [row(i, c, off) for i, c in self_wait.most_common(min(top, 10))],
            "waitStacks": stack_rows(wait_stack_weight, off)[:4],
        }
        return result

    def thread_table(self):
        rows = []
        period = 1000.0 / self.raw.get("hz", 1000.0)
        for thread in self.raw.get("threads", []):
            on = sum(r[2] for r in thread.get("runs", []) if not self.is_wait(r[1], r[3]))
            rows.append({"tid": thread["tid"], "name": thread.get("name") or "", "main": thread.get("main", False),
                         "onCpuMs": round(on * period, 1), "suspendedMs": thread.get("suspendedMs", 0.0)})
        rows.sort(key=lambda r: -r["onCpuMs"])
        return rows

    def hitches(self, top=5):
        """Each slow frame the plugin kept: where the main thread spent it and what it waited on."""
        frames = [self._function(f) for f in self.raw.get("hitchFrames", [])]
        out = []
        for hitch in self.raw.get("hitches", []):
            stacks = [[frames[i] for i in s] for s in hitch.get("stacks", [])]
            self_on, self_wait, by_stack = Counter(), Counter(), Counter()
            waiting = 0
            for _, stack, flags in hitch.get("samples", []):
                functions = stacks[stack]
                if not functions:
                    continue
                is_wait = bool(flags & FLAG_WAITING) or bool(WAIT_RE.match(self.functions[functions[0]][0]))
                if is_wait:
                    waiting += 1
                    self_wait[functions[0]] += 1
                else:
                    self_on[functions[0]] += 1
                by_stack[stack] += 1
            count = len(hitch.get("samples", []))
            dominant = None
            if by_stack:
                stack, n = by_stack.most_common(1)[0]
                dominant = {"samples": n, "stack": [self.functions[f][0] for f in reversed(stacks[stack])][-24:]}
            out.append({
                "frame": hitch.get("frame"), "ms": hitch.get("ms"), "startMs": hitch.get("startMs"), "samples": count,
                "waitingPct": round(100.0 * waiting / count, 1) if count else None,
                "topSelf": [{"function": self.functions[i][0], "module": self.functions[i][1], "samples": c}
                            for i, c in self_on.most_common(top)],
                "topWaits": [{"function": self.functions[i][0], "module": self.functions[i][1], "samples": c}
                             for i, c in self_wait.most_common(3)],
                "dominantStack": dominant, "waitChain": hitch.get("waitChain"),
            })
        return out

    def credits(self):
        used = set(self.sources)
        credits = []
        if "crashlogger" in used:
            credits.append(f"Game function names: {CRASHLOGGER_CREDIT} (CrashLogger Contributors, Nexus mod 59818)")
        if "project" in used or "project-guess" in used:
            credits.append("Project names from skyrim-research data/progress.json (names marked (guess) are unverified)")
        for source in sorted(s for s in used if s.startswith("symbols:")):
            credits.append(f"Names aligned from SYMBOLS({source.split(':', 1)[1]})")
        if "addrlib" in used:
            credits.append("id:N labels are Address Library IDs (meh321) of the containing function")
        return credits

    def summary(self, top=10):
        report = {
            "durationS": round(self.raw.get("durationS", 0.0), 2), "hz": self.raw.get("hz"),
            "threadsSpec": self.raw.get("threadsSpec"), "ticks": self.raw.get("ticks"), "lateTicks": self.raw.get("lateTicks"),
            "overhead": self.overhead(), "coverage": self.coverage(), "symbolSources": dict(self.sources),
            "main": self.report("main", top=top), "credits": self.credits(),
        }
        if self.raw.get("threadsSpec") != "main":
            report["all"] = self.report("all", top=top)
            report["threads"] = self.thread_table()[:15]
        report["hitches"] = self.hitches()
        if "context" in self.raw:
            report["context"] = self.raw["context"]
        return report

    def overhead(self):
        duration_ms = 1000.0 * max(self.raw.get("durationS", 0.0), 1e-9)
        main = next(self.threads("main"), None)
        out = {"samplerCpuMs": self.raw.get("samplerCpuMs"),
               "samplerCpuPct": round(100.0 * (self.raw.get("samplerCpuMs") or 0.0) / duration_ms, 2)}
        if main:
            out["mainSuspendedMs"] = main.get("suspendedMs")
            out["mainSuspendedPct"] = round(100.0 * main.get("suspendedMs", 0.0) / duration_ms, 2)
            out["mainWalked"] = main.get("walked")
        return out

    # ---- exports ----------------------------------------------------------------------------

    def folded(self, which="all", waiting=False):
        """Brendan Gregg's folded stacks: root;...;leaf count."""
        counts = Counter()
        for thread, _, stack, weight, is_wait, _ in self.samples(which):
            if is_wait != waiting:
                continue
            names = [self.functions[f][0].replace(";", ":") for f in reversed(self.stack_functions(stack))]
            if which == "all":
                names.insert(0, thread.get("name") or f"thread {thread['tid']}")
            counts[";".join(names)] += weight
        return "".join(f"{key} {value}\n" for key, value in sorted(counts.items()))

    def gecko(self):
        """Firefox Profiler (Gecko format) profile; open it at profiler.firefox.com or with `samply load`."""
        kind_names = ["Other", "game", "plugin", "skse", "gpu-driver", "kernel", "system", "other-dll", "unknown"]
        colors = ["grey", "blue", "green", "purple", "orange", "red", "yellow", "lightblue", "grey"]
        categories = [{"name": n, "color": c, "subcategories": ["Other"]} for n, c in zip(kind_names, colors)]
        idle_category = len(categories)
        categories.append({"name": "Idle", "color": "transparent", "subcategories": ["Other"]})
        period = 1000.0 / self.raw.get("hz", 1000.0)
        threads = []
        for thread in self.raw.get("threads", []):
            strings, string_index = [], {}

            def intern(text):
                if text not in string_index:
                    string_index[text] = len(strings)
                    strings.append(text)
                return string_index[text]

            frame_rows, frame_index = [], {}
            stack_rows, stack_index = [], {}

            def frame_of(function, idle):
                key = (function, idle)
                if key not in frame_index:
                    label, module, kind = self.functions[function]
                    category = idle_category if idle else kind_names.index(kind) if kind in kind_names else 0
                    frame_index[key] = len(frame_rows)
                    frame_rows.append([intern(f"{label} (in {module})"), False, 0, None, None, None, category, 0])
                return frame_index[key]

            def stack_of(stack, idle):
                prefix = None
                for function in reversed(self.stack_functions(stack)):
                    key = (prefix, frame_of(function, idle))
                    if key not in stack_index:
                        stack_index[key] = len(stack_rows)
                        stack_rows.append([prefix, key[1]])
                    prefix = stack_index[key]
                return prefix

            samples = []
            cache = {}
            for at, stack, count, flags in thread.get("runs", []):
                idle = self.is_wait(stack, flags)
                key = (stack, idle)
                if key not in cache:
                    cache[key] = stack_of(stack, idle)
                for i in range(count):
                    samples.append([cache[key], round(at + i * period, 3), 0])
            markers = []
            if thread.get("frameThread") or thread.get("main"):
                hitch_ms = self.raw.get("hitchThresholdMs", 100.0)
                for start, end, frame in self.raw.get("frameMarks", []):
                    name = "Hitch" if end - start > hitch_ms else "Frame"
                    markers.append([intern(name), start, end, 1, 0, {"type": name, "frame": frame, "ms": round(end - start, 2)}])
            name = thread.get("name") or ("Main thread" if thread.get("main") else f"Thread {thread['tid']}")
            threads.append({
                "name": "GeckoMain" if thread.get("main") else name, "processType": "default", "processName": "SkyrimSE.exe",
                "tid": thread["tid"], "pid": "1", "registerTime": 0, "unregisterTime": None,
                "samples": {"schema": {"stack": 0, "time": 1, "eventDelay": 2}, "data": samples},
                "markers": {"schema": {"name": 0, "startTime": 1, "endTime": 2, "phase": 3, "category": 4, "data": 5}, "data": markers},
                "stackTable": {"schema": {"prefix": 0, "frame": 1}, "data": stack_rows},
                "frameTable": {"schema": {"location": 0, "relevantForJS": 1, "innerWindowID": 2, "implementation": 3,
                                          "line": 4, "column": 5, "category": 6, "subcategory": 7}, "data": frame_rows},
                "stringTable": strings,
            })
        threads.sort(key=lambda t: t["name"] != "GeckoMain")
        meta = {
            "version": 27, "interval": period, "stackwalk": 1, "debug": 0, "startTime": self.raw.get("startEpochMs", 0),
            "shutdownTime": None, "processType": 0, "platform": "Windows", "oscpu": "Windows", "misc": "", "abi": "x86_64-msvc",
            "toolkit": "windows", "product": "SkyrimSE (skyrim-devtools profiler)", "appBuildID": "", "sourceURL": "",
            "physicalCPUs": 0, "logicalCPUs": 0, "categories": categories, "markerSchema": [],
            "extensions": {"length": 0, "baseURL": [], "id": [], "name": []},
        }
        return {"meta": meta, "libs": [], "threads": threads, "processes": [], "pausedRanges": []}


# ---- flame graph --------------------------------------------------------------------------------

KIND_HUES = {"game": (215, 60), "plugin": (130, 55), "skse": (280, 45), "gpu-driver": (30, 80), "kernel": (0, 65),
             "system": (50, 70), "other-dll": (190, 50)}


def flamegraph_svg(folded, title="Flame graph", kinds=None, width=1600, row=17):
    """A self-contained flame graph SVG from folded stacks (root at the bottom), with hover titles."""
    root = {"n": 0, "c": {}}
    for line in folded.splitlines():
        stack, _, count = line.rpartition(" ")
        if not stack:
            continue
        weight = int(count)
        node = root
        node["n"] += weight
        for name in stack.split(";"):
            node = node["c"].setdefault(name, {"n": 0, "c": {}})
            node["n"] += weight
    total = max(root["n"], 1)
    rects = []
    depth_max = 0

    def walk(node, name, x, depth):
        nonlocal depth_max
        w = width * node["n"] / total
        if w < 0.3:
            return
        depth_max = max(depth_max, depth)
        rects.append((name, x, depth, w, node["n"]))
        cx = x
        for child_name, child in sorted(node["c"].items()):
            walk(child, child_name, cx, depth + 1)
            cx += width * child["n"] / total

    walk(root, "all", 0.0, 0)
    height = (depth_max + 1) * row + 40
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="Verdana" font-size="11">',
           '<rect width="100%" height="100%" fill="#f8f8f8"/>',
           f'<text x="{width / 2}" y="18" text-anchor="middle" font-size="15">{html.escape(title)}</text>']
    for name, x, depth, w, n in rects:
        y = height - (depth + 1) * row - 4
        kind = (kinds or {}).get(name)
        hue, sat = KIND_HUES.get(kind, (zlib.crc32(name.encode()) % 60, 70))
        light = 55 + zlib.crc32(name.encode()) % 20
        pct = 100.0 * n / total
        label = html.escape(f"{name} ({n} samples, {pct:.2f}%)")
        out.append(f'<g><title>{label}</title><rect x="{x:.2f}" y="{y}" width="{w:.2f}" height="{row - 1}" '
                   f'fill="hsl({hue},{sat}%,{light}%)" rx="2"/>')
        chars = int(w / 7)
        if chars >= 3:
            text = name if len(name) <= chars else name[:chars - 2] + ".."
            out.append(f'<text x="{x + 3:.2f}" y="{y + row - 5}">{html.escape(text)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


# ---- reading and writing --------------------------------------------------------------------------


async def fetch(call):
    """Stop the running profile and read it whole; `call(method, **params)` is any RPC caller."""
    head = await call("Profile.stop")
    parts = []
    for chunk in range(head["chunks"]):
        parts.append((await call("Profile.read", chunk=chunk))["data"])
    raw = json.loads("".join(parts))
    return raw, head.get("summary", {})


def save_outputs(profile, out_dir, title="Skyrim SE"):
    """Write raw data, report, Firefox Profiler profile, folded stacks and flame graphs; returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    with gzip.open(out_dir / "raw.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(profile.raw, handle)
    paths["raw"] = out_dir / "raw.json.gz"
    summary = profile.summary()
    (out_dir / "report.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out_dir / "report.txt").write_text(render_text(summary), encoding="utf-8")
    paths["report"] = out_dir / "report.txt"
    with gzip.open(out_dir / "profile.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(profile.gecko(), handle)
    paths["firefox"] = out_dir / "profile.json.gz"
    kinds = {}
    for label, _, kind in profile.functions:
        kinds.setdefault(label, kind)
    which = "main" if profile.raw.get("threadsSpec") == "main" else "all"
    for name, waiting in (("cpu", False), ("wait", True)):
        folded = profile.folded(which, waiting=waiting)
        (out_dir / f"{name}.folded").write_text(folded, encoding="utf-8")
        svg = flamegraph_svg(folded, f"{title}: {'on-CPU' if not waiting else 'waiting (off-CPU)'} stacks, {which} threads", kinds)
        (out_dir / f"{name}-flame.svg").write_text(svg, encoding="utf-8")
        paths[f"{name}Flame"] = out_dir / f"{name}-flame.svg"
    return {k: str(v) for k, v in paths.items()}, summary


def load_raw(path):
    path = Path(path)
    if path.is_dir():
        path = path / "raw.json.gz"
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def render_text(summary, top=10):
    lines = [f"duration {summary['durationS']} s at {summary['hz']} Hz, threads={summary['threadsSpec']}"]
    overhead = summary["overhead"]
    lines.append(f"overhead: main thread suspended {overhead.get('mainSuspendedPct')}% ({overhead.get('mainSuspendedMs')} ms), "
                 f"sampler CPU {overhead.get('samplerCpuPct')}%")
    coverage = summary["coverage"]
    lines.append(f"game samples named: {coverage['namedPct']}% {coverage['split']}")
    if summary.get("context"):
        lines.append(f"context: {json.dumps(summary['context'])}")
    for which in ("main", "all"):
        report = summary.get(which)
        if not report:
            continue
        lines.append("")
        lines.append(f"== {which} threads: on-CPU {report['onCpuMs']} ms, waiting {report['waitingMs']} ms")
        lines.append("modules: " + ", ".join(f"{k} {v}%" for k, v in report["modules"].items()))
        lines.append("top self (on-CPU):")
        for r in report["topSelf"][:top]:
            lines.append(f"  {r['pct']:6.2f}%  {r['function']}  [{r['module']}]")
        lines.append("top total (inclusive):")
        for r in report["topTotal"][:top]:
            lines.append(f"  {r['pct']:6.2f}%  {r['function']}  [{r['module']}]")
        lines.append("hottest stacks:")
        for r in report["hotStacks"][:5]:
            lines.append(f"  {r['pct']:6.2f}%  " + " > ".join(r["stack"][-8:]))
        if report["topWaits"]:
            lines.append("top waits (off-CPU leaf):")
            for r in report["topWaits"][:5]:
                lines.append(f"  {r['pct']:6.2f}%  {r['function']}  [{r['module']}]")
    for hitch in summary.get("hitches", [])[:10]:
        lines.append("")
        lines.append(f"hitch frame {hitch['frame']}: {hitch['ms']} ms, {hitch['samples']} samples, waiting {hitch['waitingPct']}%")
        for r in hitch["topSelf"][:3]:
            lines.append(f"  cpu  {r['samples']:5d}  {r['function']} [{r['module']}]")
        for r in hitch["topWaits"][:3]:
            lines.append(f"  wait {r['samples']:5d}  {r['function']} [{r['module']}]")
        if hitch.get("dominantStack"):
            lines.append("  stack: " + " > ".join(hitch["dominantStack"]["stack"][-10:]))
        if hitch.get("waitChain"):
            lines.append(f"  wait chain: {json.dumps(hitch['waitChain'])}")
    if summary.get("credits"):
        lines.append("")
        lines.extend(summary["credits"])
    return "\n".join(lines) + "\n"


def diff(a, b, which="main", top=20):
    """Self and total share per function, b minus a, largest changes first."""
    ra, rb = a.report(which, top=100000), b.report(which, top=100000)

    def table(rows):
        return {(r["function"], r["module"]): r["pct"] for r in rows}

    out = {"a": {"onCpuMs": ra["onCpuMs"], "waitingMs": ra["waitingMs"], "durationS": ra["durationS"]},
           "b": {"onCpuMs": rb["onCpuMs"], "waitingMs": rb["waitingMs"], "durationS": rb["durationS"]}}
    for key in ("topSelf", "topTotal"):
        sa, sb = table(ra[key]), table(rb[key])
        changes = [{"function": f, "module": m, "a": sa.get((f, m), 0.0), "b": sb.get((f, m), 0.0),
                    "delta": round(sb.get((f, m), 0.0) - sa.get((f, m), 0.0), 2)} for f, m in set(sa) | set(sb)]
        changes.sort(key=lambda c: -abs(c["delta"]))
        out[key] = changes[:top]
    ma, mb = ra["modules"], rb["modules"]
    out["modules"] = {k: {"a": ma.get(k, 0.0), "b": mb.get(k, 0.0), "delta": round(mb.get(k, 0.0) - ma.get(k, 0.0), 2)}
                      for k in sorted(set(ma) | set(mb))}
    only_b = [c for c in out["topSelf"] if c["a"] == 0.0 and c["b"] > 0.0]
    out["newInB"] = only_b[:10]
    return out


def render_diff(result):
    lines = [f"a: on-CPU {result['a']['onCpuMs']} ms in {result['a']['durationS']} s; "
             f"b: on-CPU {result['b']['onCpuMs']} ms in {result['b']['durationS']} s",
             "modules (share of on-CPU samples, a -> b):"]
    for kind, row in result["modules"].items():
        lines.append(f"  {kind:11s} {row['a']:6.2f}% -> {row['b']:6.2f}%  ({row['delta']:+.2f})")
    for key, title in (("topSelf", "self"), ("topTotal", "total")):
        lines.append(f"largest {title} changes:")
        for c in result[key]:
            lines.append(f"  {c['delta']:+7.2f}  {c['a']:6.2f}% -> {c['b']:6.2f}%  {c['function']} [{c['module']}]")
    return "\n".join(lines) + "\n"


# ---- CLI ----------------------------------------------------------------------------------------


def default_out(stamp=None):
    base = Path(os.environ.get("SKYRIM_DEVTOOLS_PROFILE_DIR") or Path.cwd() / ".local" / "profiles")
    return base / (stamp or time.strftime("%Y%m%d-%H%M%S"))


async def drive(manager, phase):
    """A load to profile under: idle, walk (turns and walks), or coc:<cell>[,<back>] (into a cell and back,
    repeatedly; back defaults to Riverwood)."""
    if phase == "walk":
        for turn in range(100000):
            try:
                await manager.call("Player.look", yawDeg=40 if turn % 2 else -40)
                await manager.call("Move.walk", direction="forward", durationMs=2500, timeout=20)
            except Exception:  # noqa: BLE001 - a blocked walk just turns again
                await asyncio.sleep(0.5)
    elif phase.startswith("coc:"):
        cell, _, back = phase.split(":", 1)[1].partition(",")
        while True:
            for target in (cell, back or "Riverwood"):
                try:
                    await manager.call("Console.execute", command=f"coc {target}", timeout=60)
                except Exception:  # noqa: BLE001 - the reply can time out while the cell loads
                    pass
                await asyncio.sleep(8)


async def record(args):
    from .use.manager import GameManager

    config = json.loads(Path(args.config).read_text())
    manager = GameManager(config)
    symbols = GameSymbols(research_root(args.symbols, config))
    out = Path(args.out) if args.out else default_out()
    try:
        if args.attach:
            raise SystemExit("--attach is not supported: the CLI owns its game session")
        await manager.start(headless=args.headless, save=args.save)
        if args.cell:
            await manager.call("Console.execute", command=f"coc {args.cell}", timeout=60)
        await asyncio.sleep(args.settle)
        frame = (await manager.call("Perf.frameStats")).get("frame", 0)
        await manager.call("Profile.start", threads=args.threads, hz=args.hz, durationS=args.seconds + 30)
        load = asyncio.create_task(drive(manager, args.phase))
        await asyncio.sleep(args.seconds)
        load.cancel()
        stats = await manager.call("Perf.frameStats", window=args.seconds)
        raw, summary = await fetch(manager.call)
        raw["context"] = {"phase": args.phase, "fps": stats.get("fps"), "p50Ms": stats.get("p50Ms"),
                          "p99Ms": stats.get("p99Ms"), "frames": stats.get("count"), "sinceFrame": frame}
        with_name = Profile(raw, symbols)
        paths, report = save_outputs(with_name, out, title=f"Skyrim SE {args.phase}")
        print(render_text(report))
        print(json.dumps(paths, indent=1))
    finally:
        if not args.keep:
            await manager.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-profile", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="start the game, profile it, write the outputs")
    rec.add_argument("--config", required=True, type=Path)
    rec.add_argument("--seconds", type=float, default=30.0)
    rec.add_argument("--hz", type=float, default=1000.0)
    rec.add_argument("--threads", default="main", help="main, frame, all, or comma-separated ids / name fragments")
    rec.add_argument("--phase", default="idle", help="idle, walk or coc:<cell>[,<back>] (into the cell and back, repeatedly)")
    rec.add_argument("--save", default=None, help="save to load first")
    rec.add_argument("--cell", default=None, help="coc to this cell before profiling (for example Riverwood)")
    rec.add_argument("--settle", type=float, default=15.0, help="seconds to wait after loading")
    rec.add_argument("--headless", action="store_true")
    rec.add_argument("--keep", action="store_true", help="leave the game running")
    rec.add_argument("--attach", action="store_true", help=argparse.SUPPRESS)
    rec.add_argument("--symbols", default=None, help="skyrim-research checkout with the symbol tables")
    rec.add_argument("--out", default=None, help="output directory (default .local/profiles/<stamp>)")
    rep = sub.add_parser("report", help="re-symbolise a saved profile and print or rewrite its report")
    rep.add_argument("profile", help="output directory or raw.json.gz")
    rep.add_argument("--symbols", default=None)
    rep.add_argument("--config", type=Path, default=None)
    rep.add_argument("--rewrite", action="store_true", help="regenerate every output next to the raw file")
    rep.add_argument("--json", action="store_true")
    dif = sub.add_parser("diff", help="compare two profiles (for example with and without a mod)")
    dif.add_argument("a")
    dif.add_argument("b")
    dif.add_argument("--threads", default="main", help="main or all")
    dif.add_argument("--symbols", default=None)
    dif.add_argument("--config", type=Path, default=None)
    dif.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "record":
        asyncio.run(record(args))
        return 0
    config = json.loads(args.config.read_text()) if args.config else None
    symbols = GameSymbols(research_root(args.symbols, config))
    if args.command == "report":
        profile = Profile(load_raw(args.profile), symbols)
        if args.rewrite:
            target = Path(args.profile) if Path(args.profile).is_dir() else Path(args.profile).parent
            paths, summary = save_outputs(profile, target)
        else:
            summary = profile.summary()
        print(json.dumps(summary, indent=1) if args.json else render_text(summary))
        return 0
    result = diff(Profile(load_raw(args.a), symbols), Profile(load_raw(args.b), symbols), args.threads)
    print(json.dumps(result, indent=1) if args.json else render_diff(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
