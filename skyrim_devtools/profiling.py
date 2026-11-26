"""Sampling CPU profiles of the game: symbolisation, reports, flame graphs and Firefox Profiler output.

The plugin's Profile.* methods sample thread stacks and return raw addresses with module, RVA, unwind
function start and Address Library ID. Names for game code come from local symbol tables that are never
part of this package (see docs/profiling.md, Symbols): a skyrim-research checkout's CrashLogger PDB tables
and project names. Everything written here goes to a local output directory.
"""

import argparse
import asyncio
import base64
import binascii
import bisect
from collections import Counter, defaultdict
import gzip
import html
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
import zlib

CRASHLOGGER_CREDIT = "CRASHLOGGER_PDB(59818@2026.10.24)"
FLAG_WAITING = 1
FLAG_THROTTLED = 4
# DbgHelp symbol types that come from real debug information; anything else (export tables, none) only
# yields the nearest exported name, which can sit megabytes away from the sampled code.
REAL_SYMBOL_TYPES = {"pdb", "dia", "codeview", "coff", "sym"}
# An export guess is kept only this close to the sampled address (syscall stubs, short wrappers).
EXPORT_NEAR = 0x100
WAIT_RE = re.compile(
    r"^(Nt|Zw)(WaitFor|DelayExecution|RemoveIoCompletion|SignalAndWait|UserMsgWaitFor|AlertThreadByThreadId|"
    r"WaitForAlertByThreadId|ReplyWaitReceivePort|AlpcSendWaitReceivePort|UserGetMessage|GdiDdDDIWaitFor|"
    r"YieldExecution|ReadFile|DeviceIoControlFile|FsControlFile|QueryVolumeInformationFile)")
SKSE_PLUGIN_RE = re.compile(r"\\data\\skse\\plugins\\[^\\]+$", re.I)
SYSTEM_DIR_RE = re.compile(r"\\windows\\(system32|syswow64|winsxs)\\", re.I)
GPU_DRIVER_RE = re.compile(r"^(nvwgf2umx|nvldumdx|nvd3dumx|nvoglv64|amdxx64|atidxx64|amdxc64|igd10iumd64|igdumdim64|igc64|"
                           r"d3d11|d3d12|dxgi|d3d10warp|nvapi64|nvgpucomp64|igdgmm64)", re.I)
PLUGIN_NAME = "skyrimdevtools.dll"
# The build the skyrim-research symbol tables describe; a profile of another runtime reaches them through
# Address Library IDs, which name the same function in every AE build.
SYMBOLS_BUILD = "sse-steam-1-7-104"
CHUNK_TIMEOUT = 60
BUILD_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
CREDIT_RE = re.compile(r"CRASHLOGGER_PDB\(\d+@[^)\s]+\)")


# ---- symbols ------------------------------------------------------------------------------------


def research_root(explicit=None, config=None):
    """The local skyrim-research checkout that holds the symbol tables, or None."""
    for candidate in (explicit, os.environ.get("SKYRIM_DEVTOOLS_RESEARCH"), (config or {}).get("researchCheckout")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
    return None


def address_library_path(explicit=None, config=None):
    """The Address Library table of SYMBOLS_BUILD (versionlib-1-7-104-0.bin), or None."""
    for candidate in (explicit, os.environ.get("SKYRIM_DEVTOOLS_ADDRLIB"), (config or {}).get("addressLibrary")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
    return None


def read_address_library(path):
    """{id: rva} from an Address Library table: format 5 (1.7.x, a 0x60-byte header and one u32 offset per
    id) or format 2 (1.6.x, delta-encoded id/offset pairs)."""
    data = Path(path).read_bytes()
    (fmt,) = struct.unpack_from("<i", data, 0)
    if fmt == 5:
        pointer, _, count = struct.unpack_from("<iiI", data, 0x54)
        if len(data) != 0x60 + 4 * count:
            raise ValueError(f"{path}: size does not match its id count")
        return {i: o for i, o in enumerate(struct.unpack_from(f"<{count}I", data, 0x60)) if o}
    if fmt != 2:
        raise ValueError(f"{path}: unsupported Address Library format {fmt}")
    pos = 4 + 16
    (name_len,) = struct.unpack_from("<i", data, pos)
    pos += 4 + name_len
    pointer, count = struct.unpack_from("<ii", data, pos)
    pos += 8
    widths = {2: "<B", 3: "<B", 4: "<H", 5: "<H", 6: "<H", 7: "<I"}
    out = {}
    prev_id = prev_off = 0
    for _ in range(count):
        tag = data[pos]
        pos += 1
        low, high = tag & 0xF, tag >> 4
        if low == 0:
            (ident,) = struct.unpack_from("<Q", data, pos)
            pos += 8
        elif low == 1:
            ident = prev_id + 1
        else:
            (delta,) = struct.unpack_from(widths[low], data, pos)
            pos += struct.calcsize(widths[low])
            ident = {2: prev_id + delta, 3: prev_id - delta, 4: prev_id + delta, 5: prev_id - delta}.get(low, delta)
        scaled = bool(high & 8)
        base = prev_off // pointer if scaled else prev_off
        kind = high & 7
        if kind == 0:
            (offset,) = struct.unpack_from("<Q", data, pos)
            pos += 8
        elif kind == 1:
            offset = base + 1
        else:
            (delta,) = struct.unpack_from(widths[kind], data, pos)
            pos += struct.calcsize(widths[kind])
            offset = {2: base + delta, 3: base - delta, 4: base + delta, 5: base - delta}.get(kind, delta)
        if scaled:
            offset *= pointer
        out[ident] = offset
        prev_id, prev_off = ident, offset
    return out


class GameSymbols:
    """Names for game RVAs, best source first: CrashLogger PDB names, project names, sibling symbol
    alignment. Function starts come from the image's unwind table (sent by the plugin) and the PDB
    boundary table. All of these describe SYMBOLS_BUILD; with that build's Address Library table
    (`addrlib`) a profile of another runtime is named through each frame's Address Library ID."""

    def __init__(self, root=None, addrlib=None, build=None, tables=None, build_tables=True):
        self.root = Path(root) if root else None
        self.build = build or SYMBOLS_BUILD
        self.credit = CRASHLOGGER_CREDIT
        # False keeps a profile of another build on Address Library bridging even when tables exist for it,
        # to compare the two namings.
        self.build_tables = build_tables
        # Tables of a non-pinned build live outside the committed tree, in the checkout's ignored .local.
        self.tables = Path(tables) if tables else None
        self.ids = read_address_library(addrlib) if addrlib else {}
        self.names = {}
        self.project = {}
        self.aligned = {}
        self.bound_starts = []
        self.bound_sizes = []
        self.loaded = []
        if self.tables:
            self._load_pdb(self.tables)
        elif self.root:
            self._load()
        if self.ids:
            self.loaded.append(f"address library ids {len(self.ids)}")

    def _load(self):
        self._load_pdb(self.root / "reconstruction" / "generated" / "crashlogger_pdb")
        self._load_project()

    def _load_pdb(self, pdb):
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
        readme = pdb / "README.md"
        if readme.exists():
            found = CREDIT_RE.search(readme.read_text(encoding="utf-8"))
            if found:
                self.credit = found.group(0)

    def _load_project(self):
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

    def for_build(self, build):
        """The symbols to use for a profile of `build`: these when it is their build, a table set generated
        from that build's own PDB when one exists, otherwise these (named through Address Library IDs)."""
        if build == self.build or not self.build_tables or not self.root or not BUILD_RE.fullmatch(build or ""):
            return self
        tables = self.root / ".local" / "crashlogger_pdb" / build
        if not (tables / "names").is_dir():
            return self
        return GameSymbols(self.root, build=build, tables=tables)

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

    def resolve_by_id(self, ident, ident_offset, rva=None, start=None):
        """(label, source, None) for a frame of another runtime: its Address Library ID is looked up in this
        build's table and the name of the function there is used. Without a name the ID itself is the label;
        RVAs of another runtime are never looked up in these tables."""
        if ident is None or ident_offset is None or ident_offset >= 0x4000:
            return None
        if rva is not None and start is not None and rva - ident_offset < start:
            # The nearest ID lies before the frame's function, so it names a neighbour.
            return None
        label = f"id:{ident}" + (f"+0x{ident_offset:x}" if ident_offset else "")
        here = self.ids.get(ident)
        if here is not None:
            start = self.containing(here) or here
            found = self.name_at(start)
            if found:
                return found[0], f"{found[1]}@id", None
        return label, "addrlib", None

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
    if SKSE_PLUGIN_RE.search(module.get("path", "")):
        return "skse-plugin"
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
        self.modules = raw.get("modules", [])
        # A profile without a build field predates multi-runtime support and is of the pinned build.
        self.build = raw.get("build") or SYMBOLS_BUILD
        # No flag needed: a build with its own generated tables is named from them, never through another
        # build's RVAs.
        self.symbols = (symbols or GameSymbols()).for_build(self.build)
        self.foreign = self.build != self.symbols.build
        self.kinds = [module_kind(m) for m in self.modules]
        self.sources = Counter()
        self.functions = []      # (label, module name, kind)
        self.function_meta = []  # {"rva": function start (or address) RVA, "source": where the name came from}
        self.function_index = {}
        self.opaque = {}         # function index -> module name, for frames in modules without symbols
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
            if kind == "game" and self.foreign:
                label, source, _ = (self.symbols.resolve_by_id(frame.get("id"), frame.get("idOff"), frame["rva"], frame.get("start")) or
                                    (f"sub_{frame['start']:x}" if "start" in frame else f"game+0x{frame['rva']:x}",
                                     "pdata" if "start" in frame else "none", None))
            elif kind == "game":
                label, source, _ = self.symbols.resolve(frame["rva"], frame.get("start"), frame.get("id"), frame.get("idOff"))
            else:
                label, source = self._module_label(frame, module)
            key = (label, module["name"], kind)
        if count_source:
            self.sources[source] += 1
        if key not in self.function_index:
            self.function_index[key] = len(self.functions)
            self.functions.append(key)
            if source == "module-offset":
                self.opaque[self.function_index[key]] = key[1]
            start = frame.get("start")
            self.function_meta.append({"rva": start if start is not None else frame.get("rva"), "source": source})
        return self.function_index[key]

    @staticmethod
    def _module_label(frame, module):
        """Name for a frame outside the game: the DbgHelp symbol when the module has debug information (or
        the payload predates symOff), otherwise module+offset of the function start (or of the address), or module!export+offset when
        the nearest export is the function itself or a few bytes before the address."""
        name = module["name"]
        sym, offset = frame.get("sym"), frame.get("symOff")
        start = frame.get("start")
        if sym and (module.get("symbols") in REAL_SYMBOL_TYPES or offset is None):
            return sym, "dbghelp"
        if sym and offset is not None:
            export_at = frame["rva"] - offset
            if (start is not None and export_at == start) or offset < EXPORT_NEAR:
                # System DLL exports are the documented API names the wait detection and readers expect.
                return (sym if module_kind(module) in ("kernel", "system") else f"{name}!{sym}"), "export-near"
        if start is not None:
            return f"{name}+0x{start:x}", "module-offset"
        return f"{name}+0x{frame['rva']:x}", "module-offset"

    def collapse(self, functions):
        """Leaf-first function indices with each run of two or more frames from one unsymbolised module
        folded into a single "[module]" node, so driver internals read as one box."""
        out = []
        i = 0
        while i < len(functions):
            module = self.opaque.get(functions[i])
            j = i + 1
            while module is not None and j < len(functions) and self.opaque.get(functions[j]) == module:
                j += 1
            if j - i >= 2:
                kind = self.functions[functions[i]][2]
                key = (f"[{module}]", module, kind)
                if key not in self.function_index:
                    self.function_index[key] = len(self.functions)
                    self.functions.append(key)
                    self.function_meta.append({"rva": None, "source": "collapsed"})
                out.append(self.function_index[key])
            else:
                out.append(functions[i])
            i = j
        return out

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
            cached = self._stack_cache[stack] = tuple(self.collapse(out))
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
        dll_self, dll_total = Counter(), Counter()
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
            dll_self[self.functions[functions[0]][1]] += weight
            for module in {self.functions[f][1] for f in functions}:
                dll_total[module] += weight
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
            # Per DLL: self is time in its own code, total is time with it anywhere on the stack, which is
            # where a hook or callback of a mod shows even when it calls back into the game.
            "dlls": [{"module": m, "kind": self.module_kind_of(m), "selfPct": round(100.0 * dll_self[m] / on, 2),
                      "totalPct": round(100.0 * c / on, 2)} for m, c in dll_total.most_common(top)] if on else [],
            "topWaits": [row(i, c, off) for i, c in self_wait.most_common(min(top, 10))],
            "waitStacks": stack_rows(wait_stack_weight, off)[:4],
        }
        return result

    def module_kind_of(self, name):
        for module, kind in zip(self.modules, self.kinds):
            if module["name"] == name:
                return kind
        return "unknown"

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
            stacks = [self.collapse([frames[i] for i in s]) for s in hitch.get("stacks", [])]
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
        used = {source.removesuffix("@id") for source in self.sources}
        credits = []
        if self.foreign:
            bridged = sum(c for s, c in self.sources.items() if s.endswith("@id"))
            credits.append(f"Profile of {self.build}; game names are those of {self.symbols.build} reached through "
                           f"Address Library IDs ({bridged} frames); a name is a label, not proof of identity")
        if "crashlogger" in used:
            credits.append(f"Game function names: {self.symbols.credit} (CrashLogger Contributors, Nexus mod 59818)")
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
        if "gpu" in self.raw:
            from .profile_gpu import gpu_summary

            report["gpu"] = gpu_summary(self)
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
            ticks = max(self.raw.get("ticks") or 0, 1)
            out["mainThrottledPct"] = round(100.0 * main.get("throttled", 0) / ticks, 2)
        if self.raw.get("suspendBudgetPct") is not None:
            out["suspendBudgetPct"] = self.raw["suspendBudgetPct"]
        others = [t for t in self.raw.get("threads", []) if not t.get("main")]
        if others:
            worst = max(others, key=lambda t: t.get("suspendedMs", 0.0))
            out["otherThreads"] = len(others)
            out["otherHz"] = round(self.raw.get("hz", 1000.0) / max(self.raw.get("otherEvery") or 1, 1), 1)
            out["otherMaxSuspendedPct"] = round(100.0 * worst.get("suspendedMs", 0.0) / duration_ms, 2)
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

    def firefox(self):
        """Firefox Profiler profile (processed format, as samply writes it); see profile_firefox."""
        from .profile_firefox import Export

        return Export(self)


# ---- flame graph --------------------------------------------------------------------------------

KIND_HUES = {"game": (215, 60), "plugin": (130, 55), "skse": (280, 45), "gpu-driver": (30, 80), "kernel": (0, 65),
             "system": (50, 70), "other-dll": (190, 50)}


FLAME_SCRIPT = r"""
(function () {
  var svg = document.documentElement, W = +svg.getAttribute('width'), boxes = [].slice.call(document.querySelectorAll('g.f'));
  var details = document.getElementById('details'), matched = document.getElementById('matched');
  var unzoom = document.getElementById('unzoom'), total = +svg.getAttribute('data-total'), msPer = +svg.getAttribute('data-ms');
  var zoomed = null, term = null;
  function info(g) {
    var n = +g.getAttribute('data-n'), pct = 100 * n / total;
    return g.getAttribute('data-name') + ' (' + n + ' samples, ' + pct.toFixed(2) + '%' + (msPer ? ', ' + (n * msPer).toFixed(1) + ' ms' : '') + ')';
  }
  function fit(g, x, w) {
    var r = g.querySelector('rect'), t = g.querySelector('text');
    r.setAttribute('x', x.toFixed(2)); r.setAttribute('width', Math.max(w, 0).toFixed(2));
    var chars = Math.floor((w - 6) / 7), name = g.getAttribute('data-name');
    t.setAttribute('x', (x + 3).toFixed(2));
    t.textContent = chars < 3 ? '' : (name.length <= chars ? name : name.slice(0, chars - 2) + '..');
  }
  function layout() {
    var x0 = 0, w0 = 1, d0 = -1;
    if (zoomed) { x0 = +zoomed.getAttribute('data-x'); w0 = +zoomed.getAttribute('data-w'); d0 = +zoomed.getAttribute('data-d'); }
    boxes.forEach(function (g) {
      var x = +g.getAttribute('data-x'), w = +g.getAttribute('data-w'), d = +g.getAttribute('data-d');
      var inside = x >= x0 - 1e-9 && x + w <= x0 + w0 + 1e-9;
      if (zoomed && d < d0 && x <= x0 + 1e-9 && x + w >= x0 + w0 - 1e-9) { g.style.display = ''; g.classList.add('parent'); fit(g, 0, W); return; }
      g.classList.remove('parent');
      if (!inside || (zoomed && d < d0)) { g.style.display = 'none'; return; }
      var nx = (x - x0) / w0 * W, nw = w / w0 * W;
      g.style.display = nw < 0.3 ? 'none' : ''; fit(g, nx, nw);
    });
    unzoom.style.opacity = zoomed ? 1 : 0.3;
  }
  function search(text) {
    term = text || null; var hit = 0, re = null;
    try { re = term ? new RegExp(term, 'i') : null; } catch (e) { re = null; }
    var spans = [];
    boxes.forEach(function (g) {
      var on = re && re.test(g.getAttribute('data-name'));
      g.querySelector('rect').style.fill = on ? 'rgb(230,0,230)' : '';
      if (on) spans.push([+g.getAttribute('data-x'), +g.getAttribute('data-x') + +g.getAttribute('data-w')]);
    });
    spans.sort(function (a, b) { return a[0] - b[0]; });
    var end = -1;
    spans.forEach(function (s) { if (s[0] >= end) { hit += s[1] - s[0]; end = s[1]; } else if (s[1] > end) { hit += s[1] - end; end = s[1]; } });
    matched.textContent = term ? 'Matched: ' + (100 * hit).toFixed(1) + '%' : '';
  }
  boxes.forEach(function (g) {
    g.addEventListener('click', function () { zoomed = g.getAttribute('data-d') === '0' ? null : g; layout(); });
    g.addEventListener('mouseover', function () { details.textContent = info(g); });
    g.addEventListener('mouseout', function () { details.textContent = ' '; });
  });
  unzoom.addEventListener('click', function () { zoomed = null; layout(); });
  document.getElementById('search').addEventListener('click', function () {
    var t = prompt('Search (regular expression)', term || ''); if (t !== null) search(t);
  });
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); var t = prompt('Search (regular expression)', term || ''); if (t !== null) search(t); }
    else if (e.key === 'Escape') { search(''); zoomed = null; layout(); }
  });
  layout();
})();
"""


def flamegraph_svg(folded, title="Flame graph", kinds=None, width=1600, row=17, ms_per_sample=None):
    """A self-contained interactive flame graph SVG from folded stacks (root at the bottom): click a box to
    zoom into it, "Reset zoom" or Esc to go back, "Search" or Ctrl+F to highlight a regular expression;
    hovering shows samples, share and milliseconds."""
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
    boxes = []
    depth_max = 0

    # Boxes too narrow to see at full width are still kept down to a fraction of a pixel at 50x zoom.
    def walk(node, name, x, depth):
        nonlocal depth_max
        if node["n"] / total * width * 50 < 0.3:
            return
        depth_max = max(depth_max, depth)
        boxes.append((name, x, depth, node["n"]))
        cx = x
        for child_name, child in sorted(node["c"].items()):
            walk(child, child_name, cx, depth + 1)
            cx += child["n"] / total

    walk(root, "all", 0.0, 0)
    top = 40
    height = (depth_max + 1) * row + top + 26
    ms = f"{ms_per_sample:g}" if ms_per_sample else "0"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="Verdana" '
           f'font-size="11" data-total="{total}" data-ms="{ms}">',
           '<style>g.f:hover rect{stroke:#000;stroke-width:0.5;cursor:pointer} g.f text{pointer-events:none} '
           'g.parent rect{opacity:0.5} .btn{cursor:pointer;fill:#333} .btn:hover{text-decoration:underline}</style>',
           '<rect width="100%" height="100%" fill="#f8f8f8"/>',
           f'<text x="{width / 2}" y="18" text-anchor="middle" font-size="15">{html.escape(title)}</text>',
           '<text id="unzoom" class="btn" x="10" y="18" style="opacity:0.3">Reset zoom</text>',
           f'<text id="search" class="btn" x="{width - 10}" y="18" text-anchor="end">Search</text>',
           f'<text id="matched" x="{width - 10}" y="{height - 8}" text-anchor="end"> </text>',
           f'<text id="details" x="10" y="{height - 8}"> </text>']
    for name, x, depth, n in boxes:
        y = height - 26 - (depth + 1) * row
        kind = (kinds or {}).get(name)
        hue, sat = KIND_HUES.get(kind, (zlib.crc32(name.encode()) % 60, 70))
        light = 55 + zlib.crc32(name.encode()) % 20
        pct = 100.0 * n / total
        w = width * n / total
        tip = f"{name} ({n} samples, {pct:.2f}%" + (f", {n * ms_per_sample:.1f} ms" if ms_per_sample else "") + ")"
        chars = int((w - 6) / 7)
        text = "" if chars < 3 else name if len(name) <= chars else name[:chars - 2] + ".."
        out.append(f'<g class="f" data-name="{html.escape(name)}" data-x="{x:.8f}" data-w="{n / total:.8f}" data-d="{depth}" '
                   f'data-n="{n}"><title>{html.escape(tip)}</title><rect x="{x * width:.2f}" y="{y}" width="{w:.2f}" '
                   f'height="{row - 1}" fill="hsl({hue},{sat}%,{light}%)" rx="2"/>'
                   f'<text x="{x * width + 3:.2f}" y="{y + row - 5}">{html.escape(text)}</text></g>')
    out.append(f"<script><![CDATA[{FLAME_SCRIPT}]]></script>")
    out.append("</svg>")
    return "\n".join(out)


MSZIP_MAGIC = bytes.fromhex("0a51e5c0")

# ---- reading and writing --------------------------------------------------------------------------


def mszip_decode(data):
    """Inflate a Windows Compression API MSZIP buffer: a 24-byte header (magic, header size, algorithm,
    uncompressed size, block size), then per block a 32-bit length and "CK" plus a raw deflate stream that
    uses the previous 32 KiB of output as its dictionary."""
    if len(data) < 24 or data[:4] != MSZIP_MAGIC:
        raise ValueError("not an MSZIP buffer")
    total = int.from_bytes(data[8:16], "little")
    out = bytearray()
    pos = 24
    while len(out) < total:
        if pos + 4 > len(data):
            raise ValueError("MSZIP buffer is truncated")
        size = int.from_bytes(data[pos:pos + 4], "little")
        block = data[pos + 4:pos + 4 + size]
        pos += 4 + size
        if block[:2] != b"CK":
            raise ValueError("MSZIP block without its CK signature")
        inflater = zlib.decompressobj(-15, zdict=bytes(out[-32768:])) if out else zlib.decompressobj(-15)
        out += inflater.decompress(block[2:]) + inflater.flush()
    if len(out) != total:
        raise ValueError("MSZIP size mismatch")
    return bytes(out)


async def _read_payload(call, head):
    # Every chunk is requested at once: the plugin answers in order and the link stays busy.
    replies = await asyncio.gather(*(call("Profile.read", chunk=chunk) for chunk in range(head["chunks"])))
    text = "".join(reply["data"] for reply in replies)
    if head.get("encoding") == "mszip":
        return mszip_decode(base64.b64decode(text)).decode("utf-8")
    return text


async def fetch(call):
    """Stop the running profile and read it whole; `call(method, **params)` is any RPC caller.

    Returns (raw, summary); summary["transfer"] holds sizes and timings of the read."""
    began = time.monotonic()
    try:
        head = await call("Profile.stop", encoding="mszip")
    except Exception as error:  # noqa: BLE001 - a plugin older than compressed reads rejects the parameter
        if "encoding" not in str(error):
            raise
        head = await call("Profile.stop")
    stopped = time.monotonic()
    try:
        text = await _read_payload(call, head)
    except (ValueError, zlib.error, binascii.Error):
        head = await call("Profile.stop", encoding="json")
        text = await _read_payload(call, head)
    read = time.monotonic()
    raw = json.loads(text)
    summary = dict(head.get("summary") or {})
    summary["transfer"] = {
        "bytes": head.get("bytes"), "encoding": head.get("encoding", "json"),
        "encodedBytes": head.get("encodedBytes", head.get("bytes")), "chunks": head.get("chunks"),
        "waitMs": head.get("waitMs"), "serializeMs": head.get("serializeMs"), "encodeMs": head.get("encodeMs"),
        "stopS": round(stopped - began, 2), "readS": round(read - stopped, 2),
        "parseS": round(time.monotonic() - read, 2),
    }
    return raw, summary


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
    from .profile_firefox import write

    paths["firefox"] = write(profile_firefox_data(profile), out_dir / "profile.json.gz")
    kinds = {}
    for label, _, kind in profile.functions:
        kinds.setdefault(label, kind)
    which = "main" if profile.raw.get("threadsSpec") == "main" else "all"
    for name, waiting in (("cpu", False), ("wait", True)):
        folded = profile.folded(which, waiting=waiting)
        (out_dir / f"{name}.folded").write_text(folded, encoding="utf-8")
        svg = flamegraph_svg(folded, f"{title}: {'on-CPU' if not waiting else 'waiting (off-CPU)'} stacks, {which} threads", kinds,
                             ms_per_sample=1000.0 / profile.raw.get("hz", 1000.0))
        (out_dir / f"{name}-flame.svg").write_text(svg, encoding="utf-8")
        paths[f"{name}Flame"] = out_dir / f"{name}-flame.svg"
    from .profile_views import timeline_html

    (out_dir / "timeline.html").write_text(timeline_html(profile, f"{title} timeline"), encoding="utf-8")
    paths["timeline"] = out_dir / "timeline.html"
    from .profile_sqlite import export_sqlite

    export_sqlite(profile, out_dir / "profile.sqlite")
    paths["sqlite"] = out_dir / "profile.sqlite"
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
    if overhead.get("mainThrottledPct"):
        lines.append(f"  suspension budget {overhead.get('suspendBudgetPct')}%: {overhead['mainThrottledPct']}% of main-thread "
                     "ticks held the previous stack instead of suspending")
    if overhead.get("otherThreads"):
        lines.append(f"  {overhead['otherThreads']} other threads at {overhead.get('otherHz')} Hz, "
                     f"worst suspended {overhead.get('otherMaxSuspendedPct')}%")
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
        if report.get("dlls"):
            lines.append("by DLL (self / total incl. callees):")
            for r in report["dlls"][:top]:
                lines.append(f"  {r['selfPct']:6.2f}% / {r['totalPct']:6.2f}%  {r['module']} [{r['kind']}]")
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
    gpu = summary.get("gpu")
    if gpu:
        lines.append("")
        if not gpu.get("frames"):
            lines.append(f"== GPU: no timed frames ({gpu.get('reason') or 'none in this window'})")
        else:
            lines.append(f"== GPU: {gpu['frames']} of {gpu['presented']} frames timed; gpu p50 {gpu['gpuMsP50']} ms, p95 "
                         f"{gpu['gpuMsP95']} ms, max {gpu['gpuMsMax']} ms; main-thread cpu p50 {gpu['cpuMsP50']} ms; "
                         f"frame p50 {gpu['frameMsP50']} ms")
            from .profile_gpu import BOUND_LABELS

            lines.append("bound by: " + ", ".join(f"{BOUND_LABELS[k]} {v}%" for k, v in gpu["boundPct"].items()) +
                         f"; timing cost {gpu['hookUsPerFrame']} us/frame")
            if gpu.get("pacedPct"):
                busy = gpu.get("gpuBusyMsP50")
                lines.append(f"the GPU kept up with the CPU's submissions in {gpu['pacedPct']}% of frames (GPU span "
                             f"minus CPU issue span p50 {gpu.get('excessMsP50')} ms), so pass times there include "
                             f"waiting for commands; GPU busy p50 ~{busy if busy is not None else '-'} ms"
                             + (f" from {gpu['utilPctMean']}% utilisation" if gpu.get("utilPctMean") is not None else ""))
            for r in gpu["passes"]:
                lines.append(f"  {r['pct']:6.2f}%  {r['msPerFrame']:7.3f} ms  {r['pass']}  ({r['kind']})")
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
    symbols = GameSymbols(research_root(args.symbols, config), address_library_path(args.addrlib, config),
                          build_tables=not args.no_build_tables)
    out = Path(args.out) if args.out else default_out()
    try:
        if args.attach:
            raise SystemExit("--attach is not supported: the CLI owns its game session")
        await manager.start(headless=args.headless, save=args.save)
        if args.cell:
            await manager.call("Console.execute", command=f"coc {args.cell}", timeout=60)
        await asyncio.sleep(args.settle)
        frame = (await manager.call("Perf.frameStats")).get("frame", 0)
        await manager.call("Profile.start", threads=args.threads, hz=args.hz, durationS=args.seconds + 30,
                           otherHz=args.other_hz, suspendBudgetPct=args.suspend_budget)
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


async def top(args):
    from .profile_views import render_top, top_snapshot
    from .use.manager import GameManager

    config = json.loads(Path(args.config).read_text())
    manager = GameManager(config)
    symbols = GameSymbols(research_root(args.symbols, config))
    interactive = sys.stdout.isatty() and not args.json
    load = None
    try:
        await manager.start(headless=args.headless, save=args.save)
        if args.cell:
            await manager.call("Console.execute", command=f"coc {args.cell}", timeout=60)
        if args.phase != "idle":
            load = asyncio.create_task(drive(manager, args.phase))
        shown = 0
        while not args.count or shown < args.count:
            started = time.monotonic()
            snap = await top_snapshot(manager.call, symbols, seconds=args.window, top=args.top)
            if args.json:
                print(json.dumps(snap), flush=True)
            else:
                print(("\x1b[H\x1b[2J" if interactive else "") + render_top(snap) + ("" if interactive else "\n"), flush=True)
            shown += 1
            await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        if load:
            load.cancel()
        if not args.keep:
            await manager.stop()


def export_command(args, symbols):
    from . import profile_firefox

    source = Path(args.profile)
    raw = load_raw(source)
    if args.recorder and raw.get("startEpochMs"):
        from .profile_views import recorder_marks

        raw.setdefault("marks", []).extend(recorder_marks(args.recorder, raw["startEpochMs"], raw.get("durationS", 0) * 1000.0))
    profile = Profile(raw, symbols)
    if args.sqlite:
        from .profile_sqlite import export_sqlite

        counts = export_sqlite(profile, args.sqlite)
        print(json.dumps({"sqlite": str(args.sqlite), "rows": counts}))
        if not args.firefox and not args.open:
            return 0
    out = Path(args.firefox) if args.firefox else (source if source.is_dir() else source.parent) / "profile.json.gz"
    data = profile_firefox_data(profile)
    problems = profile_firefox.check(data)
    if problems:
        raise SystemExit("Firefox export failed its checks: " + "; ".join(problems))
    profile_firefox.write(data, out)
    print(f"wrote {out}")
    if args.open:
        server, url = profile_firefox.serve(out, open_browser=not args.no_browser)
        print(f"serving on 127.0.0.1 for {url}\n(the browser reads it from this machine; do not press Upload). Ctrl+C stops.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
            server.server_close()
    return 0


def profile_firefox_data(profile):
    """The Firefox profile with everything this build records; save_outputs and export share it."""
    from .profile_gpu import firefox_tracks

    export = profile.firefox()
    firefox_tracks(export, profile)
    return export.build()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-profile", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="start the game, profile it, write the outputs")
    rec.add_argument("--config", required=True, type=Path)
    rec.add_argument("--seconds", type=float, default=30.0)
    rec.add_argument("--hz", type=float, default=1000.0)
    rec.add_argument("--threads", default="all", help="main, frame, all, or comma-separated ids / name fragments")
    rec.add_argument("--other-hz", type=float, default=250.0, help="sampling rate for threads other than the main one")
    rec.add_argument("--suspend-budget", type=float, default=2.5,
                     help="percent of a thread's time the sampler may keep it suspended (0: no limit)")
    rec.add_argument("--phase", default="idle", help="idle, walk or coc:<cell>[,<back>] (into the cell and back, repeatedly)")
    rec.add_argument("--save", default=None, help="save to load first")
    rec.add_argument("--cell", default=None, help="coc to this cell before profiling (for example Riverwood)")
    rec.add_argument("--settle", type=float, default=15.0, help="seconds to wait after loading")
    rec.add_argument("--headless", action="store_true")
    rec.add_argument("--keep", action="store_true", help="leave the game running")
    rec.add_argument("--attach", action="store_true", help=argparse.SUPPRESS)
    rec.add_argument("--addrlib", default=None, help="versionlib-1-7-104-0.bin, to name profiles of other runtimes")
    rec.add_argument("--no-build-tables", action="store_true",
                    help="name other builds through --addrlib even when tables generated from their PDB exist")
    rec.add_argument("--symbols", default=None, help="skyrim-research checkout with the symbol tables")
    rec.add_argument("--out", default=None, help="output directory (default .local/profiles/<stamp>)")
    rep = sub.add_parser("report", help="re-symbolise a saved profile and print or rewrite its report")
    rep.add_argument("profile", help="output directory or raw.json.gz")
    rep.add_argument("--addrlib", default=None, help="versionlib-1-7-104-0.bin, to name profiles of other runtimes")
    rep.add_argument("--no-build-tables", action="store_true",
                    help="name other builds through --addrlib even when tables generated from their PDB exist")
    rep.add_argument("--symbols", default=None)
    rep.add_argument("--config", type=Path, default=None)
    rep.add_argument("--rewrite", action="store_true", help="regenerate every output next to the raw file")
    rep.add_argument("--json", action="store_true")
    exp = sub.add_parser("export", help="write the Firefox Profiler file of a saved profile; --open serves it to "
                                        "profiler.firefox.com from this machine")
    exp.add_argument("profile", help="output directory or raw.json.gz")
    exp.add_argument("--firefox", default=None, help="output .json or .json.gz (default <dir>/profile.json.gz)")
    exp.add_argument("--open", action="store_true", help="serve on 127.0.0.1 and open profiler.firefox.com on it "
                                                         "(nothing is uploaded; never press the site's Upload button)")
    exp.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    exp.add_argument("--addrlib", default=None, help="versionlib-1-7-104-0.bin, to name profiles of other runtimes")
    exp.add_argument("--no-build-tables", action="store_true",
                    help="name other builds through --addrlib even when tables generated from their PDB exist")
    exp.add_argument("--sqlite", default=None, help="also (or only) write the profile as a SQLite database")
    exp.add_argument("--recorder", default=None, help="skyrim-use timeline .jsonl whose tool calls and narration to add as markers")
    exp.add_argument("--symbols", default=None)
    exp.add_argument("--config", type=Path, default=None)
    dif = sub.add_parser("diff", help="compare two profiles (for example with and without a mod)")
    dif.add_argument("a")
    dif.add_argument("b")
    dif.add_argument("--threads", default="main", help="main or all")
    dif.add_argument("--addrlib", default=None, help="versionlib-1-7-104-0.bin, to name profiles of other runtimes")
    dif.add_argument("--no-build-tables", action="store_true",
                    help="name other builds through --addrlib even when tables generated from their PDB exist")
    dif.add_argument("--symbols", default=None)
    dif.add_argument("--config", type=Path, default=None)
    dif.add_argument("--json", action="store_true")
    tp = sub.add_parser("top", help="start the game and show a live dashboard (fps, hot functions, threads, memory, GPU)")
    tp.add_argument("--config", required=True, type=Path)
    tp.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes")
    tp.add_argument("--window", type=float, default=3.0, help="seconds of main-thread samples per view")
    tp.add_argument("--top", type=int, default=15)
    tp.add_argument("--count", type=int, default=0, help="stop after this many refreshes (0: until Ctrl-C)")
    tp.add_argument("--json", action="store_true", help="one JSON snapshot per line instead of the dashboard")
    tp.add_argument("--phase", default="idle", help="idle, walk or coc:<cell>[,<back>]")
    tp.add_argument("--save", default=None)
    tp.add_argument("--cell", default=None)
    tp.add_argument("--headless", action="store_true")
    tp.add_argument("--keep", action="store_true")
    tp.add_argument("--symbols", default=None)
    tl = sub.add_parser("timeline", help="write the HTML timeline of a saved profile")
    tl.add_argument("profile", help="output directory or raw.json.gz")
    tl.add_argument("--out", default=None, help="HTML file (default timeline.html next to the raw file)")
    tl.add_argument("--recorder", default=None, help="skyrim-use timeline .jsonl whose tool calls and narration to overlay")
    tl.add_argument("--symbols", default=None)
    tl.add_argument("--config", type=Path, default=None)
    qu = sub.add_parser("query", help="run one read-only SQL statement against an exported profile")
    qu.add_argument("database", help="profile.sqlite, or an output directory that holds one")
    qu.add_argument("sql")
    qu.add_argument("--limit", type=int, default=200)
    qu.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "query":
        import sqlite3

        from .profile_sqlite import query

        database = Path(args.database)
        try:
            result = query(database / "profile.sqlite" if database.is_dir() else database, args.sql, args.limit)
        except (sqlite3.Error, FileNotFoundError) as error:
            print(f"query failed: {error} (read-only: SELECT only)", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            print("\t".join(result["columns"]))
            for row in result["rows"]:
                print("\t".join("" if v is None else str(v) for v in row))
            if result["truncated"]:
                print(f"(first {args.limit} rows)")
        return 0
    if args.command == "record":
        asyncio.run(record(args))
        return 0
    if args.command == "top":
        try:
            asyncio.run(top(args))
        except KeyboardInterrupt:
            pass
        return 0
    config = json.loads(args.config.read_text()) if args.config else None
    symbols = GameSymbols(research_root(args.symbols, config), address_library_path(args.addrlib, config),
                          build_tables=not args.no_build_tables)
    if args.command == "timeline":
        from .profile_views import recorder_marks, timeline_html

        raw = load_raw(args.profile)
        if args.recorder and raw.get("startEpochMs"):
            raw.setdefault("marks", []).extend(recorder_marks(args.recorder, raw["startEpochMs"], raw.get("durationS", 0) * 1000.0))
        target = Path(args.out) if args.out else (Path(args.profile) if Path(args.profile).is_dir() else Path(args.profile).parent) / "timeline.html"
        target.write_text(timeline_html(Profile(raw, symbols)), encoding="utf-8")
        print(target)
        return 0
    if args.command == "report":
        profile = Profile(load_raw(args.profile), symbols)
        if args.rewrite:
            target = Path(args.profile) if Path(args.profile).is_dir() else Path(args.profile).parent
            paths, summary = save_outputs(profile, target)
        else:
            summary = profile.summary()
        print(json.dumps(summary, indent=1) if args.json else render_text(summary))
        return 0
    if args.command == "export":
        return export_command(args, symbols)
    result = diff(Profile(load_raw(args.a), symbols), Profile(load_raw(args.b), symbols), args.threads)
    print(json.dumps(result, indent=1) if args.json else render_diff(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
