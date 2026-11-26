"""I/O and load traces of the game: SQLite tables, a Perfetto timeline and a text report from an IoTrace.dump.

The plugin's I/O trace (native/iotrace.cpp, armed with SKYRIM_DEVTOOLS_IOTRACE) records every file open, read,
seek, mapping and DLL load in the game process with its file, size, latency, thread and call stack, and samples
page faults, memory and per-thread CPU time. IoTrace.dump writes `iotrace-<n>[-label].bin` (fixed 48-byte
records) and a `.json` sidecar into the run directory. This module reads that pair; it uploads nothing and writes
only to the output directory it is given (see docs/profiling.md, I/O traces).
"""

import argparse
import bisect
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys

from skyrim_devtools.profiling import GameSymbols, address_library_path, research_root

EVENT = struct.Struct("<qIIQIIIHBBQ")
OPS = {1: "open", 2: "read", 3: "read-issue", 4: "read-wait", 5: "seek", 6: "mapping", 7: "map-view", 8: "close",
       9: "nt-read", 10: "load-library", 11: "plugin-load"}
READ_OPS = {2, 3, 9}
FLAG_FAILED, FLAG_OVERLAPPED, FLAG_NOBUF, FLAG_SEQ, FLAG_RANDOM, FLAG_WRITE, FLAG_PENDING, FLAG_NESTED = (
    1, 2, 4, 8, 16, 32, 64, 128)
SIZE_BUCKETS = [512, 4096, 16384, 65536, 262144, 1 << 20, 4 << 20]
# Frames that belong to the hook or to the OS file layer, skipped when naming the caller of an I/O call.
SKIP_MODULE_RE = re.compile(r"(^|\\)(!?skyrimdevtools\.dll|kernelbase\.dll|kernel32\.dll|ntdll\.dll|usvfs_x64\.dll|"
                            r"ucrtbase\.dll|msvcp140\.dll|vcruntime140\.dll|vcruntime140_1\.dll)$", re.I)
MOD_RE = re.compile(r"\\mods\\([^\\]+)\\", re.I)
ARCHIVE_EXT = {".bsa", ".ba2"}
PLUGIN_EXT = {".esp", ".esm", ".esl"}


@dataclass
class Event:
    t: float          # seconds since the trace was armed
    dur: float        # seconds
    op: int
    file: int
    offset: int
    size: int
    result: int
    stack: int
    thread: int
    flags: int
    extra: int


def file_kind(path):
    lower = path.lower()
    ext = os.path.splitext(lower)[1]
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in PLUGIN_EXT:
        return "plugin"
    if ext in {".dll", ".exe"}:
        return "module"
    if ext in {".nif", ".dds", ".hkx", ".tri", ".btr", ".bto", ".pex", ".seq", ".wav", ".xwm", ".fuz", ".lip", ".swf",
               ".ini", ".toml", ".json", ".txt", ".bin", ".esp", ".strings", ".dlstrings", ".ilstrings"}:
        return ext[1:]
    if "\\windows\\" in lower or "\\programdata\\" in lower:
        return "system"
    return ext[1:] if ext else "other"


def mod_of(physical):
    match = MOD_RE.search(physical or "")
    if match:
        return match.group(1)
    lower = (physical or "").lower()
    if "\\overwrite\\" in lower:
        return "(overwrite)"
    if "\\data\\" in lower:
        return "(game data)"
    return ""


class IoTrace:
    """One dump: events, files, threads, stacks resolved to callers, counters, marks and phases."""

    def __init__(self, meta, blob, symbols=None):
        self.meta = meta
        self.symbols = symbols
        self.freq = float(meta["qpcFrequency"])
        self.base = int(meta["armedAt"])
        if meta.get("eventSize", EVENT.size) != EVENT.size:
            raise ValueError(f"event size {meta.get('eventSize')} is not {EVENT.size}")
        self.events = []
        for start, ticks, file, offset, size, result, stack, thread, flags, op, extra in EVENT.iter_unpack(blob):
            if op:
                self.events.append(Event((start - self.base) / self.freq, ticks / self.freq, op, file, offset, size,
                                         result, stack, thread, flags, extra))
        self.events.sort(key=lambda e: e.t)
        self._times = [e.t for e in self.events]
        self.files = [(f[0], f[1]) for f in meta.get("files", [])]
        self.thread_ids = meta.get("threads", [0])
        names = meta.get("threadNames", {})
        self.thread_names = {i: names.get(str(tid), "") for i, tid in enumerate(self.thread_ids)}
        self.marks = sorted(((at - self.base) / self.freq, name) for at, name in meta.get("marks", []))
        self.modules = meta.get("modules", [])
        self.callers = self._callers()
        self.phases = self._phases()

    @classmethod
    def load(cls, path, symbols=None):
        """`path` is the .json sidecar, the .bin, or their common stem."""
        path = Path(path)
        stem = path.with_suffix("") if path.suffix in (".json", ".bin", ".gz") else path
        if stem.suffix == ".json":
            stem = stem.with_suffix("")
        meta = json.loads(_read(stem.with_suffix(".json")))
        return cls(meta, _read(stem.with_suffix(".bin"), binary=True), symbols)

    def seconds(self, qpc):
        return (qpc - self.base) / self.freq

    # ---- names ---------------------------------------------------------------------------------

    def frame_label(self, frame):
        module = frame.get("module")
        if module is None:
            return f"0x{frame.get('address', 0):x}"
        path = self.modules[module]["path"] if module < len(self.modules) else "?"
        name = path.rsplit("\\", 1)[-1]
        game = self.modules[module]["base"] == self.meta.get("gameBase") if module < len(self.modules) else False
        if game:
            if self.symbols and frame.get("id") is not None:
                found = self.symbols.resolve_by_id(frame["id"], frame.get("idOffset", 0))
                if found:
                    return found[0]
            if frame.get("id") is not None:
                return f"id:{frame['id']}+0x{frame.get('idOffset', 0):x}"
            return f"{name}+0x{frame['rva']:x}"
        if frame.get("name"):
            return f"{name}!{frame['name']}"
        return f"{name}+0x{frame['rva']:x}"

    def _callers(self):
        """Per stack id: (caller label, caller module name, full label chain without hook/OS frames)."""
        frames = self.meta.get("frames", [])
        out = {0: ("?", "?", [])}
        for sid, row in enumerate(self.meta.get("stacks", [])):
            if sid == 0:
                continue
            chain = []
            module_name = "?"
            for index in row:
                frame = frames[index] if index < len(frames) else {}
                m = frame.get("module")
                path = self.modules[m]["path"] if m is not None and m < len(self.modules) else ""
                if path and SKIP_MODULE_RE.search(path):
                    continue
                if not chain:
                    module_name = path.rsplit("\\", 1)[-1] if path else "?"
                chain.append(self.frame_label(frame))
            out[sid] = (chain[0] if chain else "?", module_name, chain)
        return out

    # ---- phases --------------------------------------------------------------------------------

    def mark_time(self, prefix, after=-1.0):
        for t, name in self.marks:
            if t > after and name.startswith(prefix):
                return t
        return None

    def _phases(self):
        end = max([e.t + e.dur for e in self.events] + [t for t, _ in self.marks] + [0.0])
        phases = []
        post_load = self.mark_time("skse PostLoad")
        data_loaded = self.mark_time("skse DataLoaded")
        menu = self.mark_time("Main Menu open")
        startup_end = menu or data_loaded or end
        phases.append(("startup", 0.0, startup_end))
        if post_load is not None:
            phases.append(("skse-plugins", 0.0, post_load))
        if post_load is not None and data_loaded is not None:
            begin = self.mark_time("skse InputLoaded") or post_load
            phases.append(("data-load", begin, data_loaded))
        if data_loaded is not None and menu is not None and menu > data_loaded:
            phases.append(("to-main-menu", data_loaded, menu))
        for t, name in self.marks:
            if name.startswith("coc ") and name.endswith(" begin"):
                cell = name[4:-6]
                stop = self.mark_time(f"coc {cell} end", t)
                if stop is not None:
                    phases.append((f"load {cell}", t, stop))
        return phases

    def in_phase(self, phase):
        _, a, b = phase
        return self.events[bisect.bisect_left(self._times, a):bisect.bisect_left(self._times, b)]

    # ---- counters ------------------------------------------------------------------------------

    def counter_rows(self):
        columns = self.meta.get("counterColumns", [])
        rows = []
        for row in self.meta.get("counters", []):
            item = dict(zip(columns, row))
            item["t"] = self.seconds(item.pop("at"))
            rows.append(item)
        return rows

    def counter_delta(self, a, b):
        rows = self.counter_rows()
        if not rows:
            return {}
        times = [r["t"] for r in rows]
        i = max(0, bisect.bisect_right(times, a) - 1)
        j = min(len(rows) - 1, bisect.bisect_left(times, b))
        first, last = rows[i], rows[j]
        out = {}
        for key in ("pageFaults", "readOps", "readBytes", "sysPageReads", "sysPageReadIos", "sysTransition",
                    "sysDemandZero", "sysCacheTransition", "sysFaults"):
            if key in first:
                delta = last[key] - first[key]
                if key.startswith("sys") and delta < 0:
                    delta += 1 << 32  # 32-bit system counters wrap
                out[key] = delta
        for key in ("workingSet", "privateBytes", "availPhys"):
            if key in first:
                out[key + "Start"] = first[key]
                out[key + "End"] = last[key]
                out[key + "Min"] = min(r[key] for r in rows[i:j + 1])
        return out

    def thread_cpu(self, a, b):
        """CPU seconds per OS thread id between a and b, from the 250 ms samples."""
        by_thread = defaultdict(list)
        for at, tid, cycles, cpu in self.meta.get("threadSamples", []):
            by_thread[tid].append((self.seconds(at), cpu))
        out = {}
        for tid, samples in by_thread.items():
            inside = [s for s in samples if a - 0.3 <= s[0] <= b + 0.3]
            if len(inside) >= 2:
                out[tid] = (inside[-1][1] - inside[0][1]) / 1e7
        return out

    # ---- summaries -----------------------------------------------------------------------------

    def file_stats(self, events):
        stats = {}
        last_end = {}
        for e in events:
            if e.op not in READ_OPS and e.op not in (1, 5, 6, 7):
                continue
            s = stats.setdefault(e.file, Counter())
            if e.op == 1:
                s["opens"] += 1
                s["openS"] += e.dur
                if e.offset:
                    s["fileSize"] = max(s["fileSize"], e.offset)
            elif e.op in READ_OPS:
                if e.flags & FLAG_NESTED:
                    continue
                s["reads"] += 1
                s["readS"] += e.dur
                s["bytes"] += e.result or e.size
                key = (e.file, e.extra if e.op != 3 else e.file)
                if e.offset != 0xFFFFFFFFFFFFFFFF:
                    if key in last_end and e.offset != last_end[key]:
                        s["nonSequential"] += 1
                    last_end[key] = e.offset + (e.result or e.size)
                if (e.result or e.size) < 4096:
                    s["smallReads"] += 1
            elif e.op == 5:
                s["seeks"] += 1
            elif e.op == 7:
                s["views"] += 1
                s["viewBytes"] += e.size
        return stats

    def size_histogram(self, events):
        hist = Counter()
        time_hist = Counter()
        for e in events:
            if e.op in READ_OPS and not e.flags & FLAG_NESTED:
                n = e.result or e.size
                bucket = next((b for b in SIZE_BUCKETS if n <= b), "larger")
                hist[bucket] += 1
                time_hist[bucket] += e.dur
        return hist, time_hist

    def summary(self, top=15):
        out = {"armedEpochMs": self.meta.get("armedEpochMs"), "events": len(self.events),
               "dropped": self.meta.get("dropped", 0), "hookOverheadS": self.meta.get("hookOverheadS"),
               "hooks": self.meta.get("hooks", {}), "marks": [[round(t, 3), n] for t, n in self.marks], "phases": []}
        for phase in self.phases:
            name, a, b = phase
            events = self.in_phase(phase)
            reads = [e for e in events if e.op in READ_OPS and not e.flags & FLAG_NESTED]
            waits = [e for e in events if e.op == 4]
            stats = self.file_stats(events)
            per_kind = defaultdict(Counter)
            per_mod = defaultdict(Counter)
            for fid, s in stats.items():
                path, physical = self.files[fid] if fid < len(self.files) else ("?", "")
                kind = file_kind(path)
                for k in ("reads", "readS", "bytes", "opens", "openS"):
                    per_kind[kind][k] += s[k]
                    per_mod[mod_of(physical)][k] += s[k]
            by_thread = defaultdict(Counter)
            for e in events:
                if e.op in READ_OPS or e.op in (1, 4, 7):
                    by_thread[e.thread]["ioS"] += e.dur
                    by_thread[e.thread]["calls"] += 1
            callers = defaultdict(Counter)
            for e in reads:
                caller = self.callers.get(e.stack, ("?", "?", []))[0]
                callers[caller]["reads"] += 1
                callers[caller]["readS"] += e.dur
                callers[caller]["bytes"] += e.result or e.size
            cpu = self.thread_cpu(a, b)
            hist, time_hist = self.size_histogram(events)
            top_files = sorted(stats.items(), key=lambda kv: -(kv[1]["readS"] + kv[1]["openS"]))[:top]
            item = {
                "name": name, "start": round(a, 3), "end": round(b, 3), "seconds": round(b - a, 3),
                "opens": sum(1 for e in events if e.op == 1), "openS": round(sum(e.dur for e in events if e.op == 1), 3),
                "failedOpens": sum(1 for e in events if e.op == 1 and e.flags & FLAG_FAILED),
                "reads": len(reads), "readS": round(sum(e.dur for e in reads), 3),
                "readBytes": sum(e.result or e.size for e in reads),
                "overlappedWaits": len(waits), "overlappedWaitS": round(sum(e.dur for e in waits), 3),
                "mapViews": sum(1 for e in events if e.op == 7),
                "counters": self.counter_delta(a, b),
                "sizeHistogram": {str(k): [hist[k], round(time_hist[k], 3)] for k in SIZE_BUCKETS + ["larger"] if hist[k]},
                "byKind": {k: {kk: (round(v, 3) if isinstance(v, float) else v) for kk, v in c.items()}
                           for k, c in sorted(per_kind.items(), key=lambda kv: -kv[1]["readS"])},
                "byMod": [[m, round(c["readS"] + c["openS"], 3), c["reads"], c["bytes"]]
                          for m, c in sorted(per_mod.items(), key=lambda kv: -(kv[1]["readS"] + kv[1]["openS"]))[:top]],
                "topFiles": [self._file_row(fid, s) for fid, s in top_files],
                "topCallers": [[c, v["reads"], round(v["readS"], 3), v["bytes"]]
                               for c, v in sorted(callers.items(), key=lambda kv: -kv[1]["readS"])[:top]],
                "threads": [self._thread_row(t, c, cpu, b - a) for t, c in
                            sorted(by_thread.items(), key=lambda kv: -kv[1]["ioS"])[:top]],
                "busiestCpuThreads": [[tid, self._tid_name(tid), round(s, 2), round(s / max(b - a, 1e-9), 3)]
                                      for tid, s in sorted(cpu.items(), key=lambda kv: -kv[1])[:top]],
            }
            out["phases"].append(item)
        out["plugins"] = self.plugin_times()
        out["dlls"] = self.dll_times()[:60]
        return out

    def _tid_name(self, tid):
        return self.meta.get("threadNames", {}).get(str(tid), "")

    def _thread_row(self, index, counter, cpu, seconds):
        tid = self.thread_ids[index] if index < len(self.thread_ids) else 0
        return [tid, self.thread_names.get(index, ""), counter["calls"], round(counter["ioS"], 3),
                round(cpu.get(tid, 0.0), 2), round(cpu.get(tid, 0.0) / max(seconds, 1e-9), 3)]

    def _file_row(self, fid, s):
        path, physical = self.files[fid] if fid < len(self.files) else ("?", "")
        return {"file": path, "mod": mod_of(physical), "kind": file_kind(path), "opens": s["opens"],
                "openS": round(s["openS"], 4), "reads": s["reads"], "readS": round(s["readS"], 4), "bytes": s["bytes"],
                "nonSequential": s["nonSequential"], "smallReads": s["smallReads"], "seeks": s["seeks"],
                "fileSize": s["fileSize"]}

    def plugin_times(self):
        """Per plugin file (.esp/.esm/.esl): read time and the span from its first to its last read on the
        thread that read it most; consecutive spans on that thread approximate time spent per plugin."""
        reads = defaultdict(list)
        for e in self.events:
            if e.op in READ_OPS or e.op == 1:
                path = self.files[e.file][0] if e.file < len(self.files) else ""
                if os.path.splitext(path.lower())[1] in PLUGIN_EXT:
                    reads[e.file].append(e)
        rows = []
        for fid, events in reads.items():
            thread = Counter(e.thread for e in events).most_common(1)[0][0]
            mine = [e for e in events if e.thread == thread]
            first = min(e.t for e in mine)
            last = max(e.t + e.dur for e in mine)
            rows.append({"plugin": self.files[fid][0].rsplit("\\", 1)[-1], "mod": mod_of(self.files[fid][1]),
                         "first": round(first, 4), "spanS": round(last - first, 4),
                         "readS": round(sum(e.dur for e in events if e.op in READ_OPS), 4),
                         "reads": sum(1 for e in events if e.op in READ_OPS),
                         "bytes": sum(e.result or e.size for e in events if e.op in READ_OPS),
                         "opens": sum(1 for e in events if e.op == 1),
                         "thread": self.thread_ids[thread] if thread < len(self.thread_ids) else 0})
        rows.sort(key=lambda r: r["first"])
        return rows

    def dll_times(self):
        rows = {}
        for e in self.events:
            if e.op == 10:
                name = self.files[e.file][0].rsplit("\\", 1)[-1] if e.file < len(self.files) else "?"
                row = rows.setdefault(name.lower(), {"dll": name, "loadLibraryS": 0.0, "pluginLoadS": 0.0, "at": e.t})
                row["loadLibraryS"] += e.dur
            elif e.op == 11:
                name = self.files[e.file][0].rsplit("\\", 1)[-1] if e.file < len(self.files) else "?"
                row = rows.setdefault(name.lower(), {"dll": name, "loadLibraryS": 0.0, "pluginLoadS": 0.0, "at": e.t})
                row["pluginLoadS"] += e.dur
        out = sorted(rows.values(), key=lambda r: -(r["loadLibraryS"] + r["pluginLoadS"]))
        for r in out:
            r["loadLibraryS"] = round(r["loadLibraryS"], 4)
            r["pluginLoadS"] = round(r["pluginLoadS"], 4)
            r["at"] = round(r["at"], 3)
        return out


def _read(path, binary=False):
    path = Path(path)
    if not path.exists() and Path(str(path) + ".gz").exists():
        path = Path(str(path) + ".gz")
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return data if binary else data.decode("utf-8")


# ---- outputs ---------------------------------------------------------------------------------------


def write_sqlite(trace, path):
    path = Path(path)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE io_files (id INTEGER PRIMARY KEY, path TEXT, physical TEXT, kind TEXT, mod TEXT);
        CREATE TABLE io_threads (idx INTEGER PRIMARY KEY, tid INTEGER, name TEXT);
        CREATE TABLE io_stacks (id INTEGER PRIMARY KEY, caller TEXT, module TEXT, frames TEXT);
        CREATE TABLE io_events (t REAL, dur REAL, op TEXT, file INTEGER, offset INTEGER, size INTEGER, result INTEGER,
                                stack INTEGER, thread INTEGER, flags INTEGER, extra INTEGER);
        CREATE TABLE io_marks (t REAL, name TEXT);
        CREATE TABLE io_phases (name TEXT, start REAL, "end" REAL);
        CREATE TABLE io_counters (t REAL, page_faults INTEGER, working_set INTEGER, private_bytes INTEGER,
                                  avail_phys INTEGER, read_ops INTEGER, read_bytes INTEGER, sys_page_reads INTEGER,
                                  sys_page_read_ios INTEGER, sys_transition INTEGER, sys_demand_zero INTEGER);
        CREATE TABLE io_thread_cpu (t REAL, tid INTEGER, cycles INTEGER, cpu_s REAL);
        CREATE TABLE io_dlls (dll TEXT, at REAL, load_library_s REAL, plugin_load_s REAL);
        CREATE TABLE io_plugins (plugin TEXT, mod TEXT, first REAL, span_s REAL, read_s REAL, reads INTEGER,
                                 bytes INTEGER, thread INTEGER);
    """)
    db.executemany("INSERT INTO io_files VALUES (?,?,?,?,?)",
                   [(i, p, ph, file_kind(p), mod_of(ph)) for i, (p, ph) in enumerate(trace.files)])
    db.executemany("INSERT INTO io_threads VALUES (?,?,?)",
                   [(i, tid, trace.thread_names.get(i, "")) for i, tid in enumerate(trace.thread_ids)])
    db.executemany("INSERT INTO io_stacks VALUES (?,?,?,?)",
                   [(sid, c[0], c[1], " <- ".join(c[2])) for sid, c in trace.callers.items()])
    db.executemany("INSERT INTO io_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   [(e.t, e.dur, OPS.get(e.op, str(e.op)), e.file, e.offset if e.offset < (1 << 63) else -1, e.size,
                     e.result, e.stack, trace.thread_ids[e.thread] if e.thread < len(trace.thread_ids) else 0, e.flags,
                     e.extra if e.extra < (1 << 63) else -1) for e in trace.events])
    db.executemany("INSERT INTO io_marks VALUES (?,?)", trace.marks)
    db.executemany("INSERT INTO io_phases VALUES (?,?,?)", trace.phases)
    db.executemany("INSERT INTO io_counters VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   [(r["t"], r.get("pageFaults"), r.get("workingSet"), r.get("privateBytes"), r.get("availPhys"),
                     r.get("readOps"), r.get("readBytes"), r.get("sysPageReads"), r.get("sysPageReadIos"),
                     r.get("sysTransition"), r.get("sysDemandZero")) for r in trace.counter_rows()])
    db.executemany("INSERT INTO io_thread_cpu VALUES (?,?,?,?)",
                   [(trace.seconds(at), tid, cycles, cpu / 1e7) for at, tid, cycles, cpu in trace.meta.get("threadSamples", [])])
    db.executemany("INSERT INTO io_dlls VALUES (?,?,?,?)",
                   [(r["dll"], r["at"], r["loadLibraryS"], r["pluginLoadS"]) for r in trace.dll_times()])
    db.executemany("INSERT INTO io_plugins VALUES (?,?,?,?,?,?,?,?)",
                   [(r["plugin"], r["mod"], r["first"], r["spanS"], r["readS"], r["reads"], r["bytes"], r["thread"])
                    for r in trace.plugin_times()])
    db.commit()
    db.close()


def perfetto_trace(trace, min_us=50.0):
    """Chrome trace-event JSON (opens in ui.perfetto.dev or chrome://tracing): one track per thread with its I/O
    calls of at least `min_us`, a phase track, marks, and counter tracks for faults, working set and reads."""
    pid = 1
    events = [{"ph": "M", "pid": pid, "name": "process_name", "args": {"name": "SkyrimSE I/O"}},
              {"ph": "M", "pid": pid, "tid": 0, "name": "thread_name", "args": {"name": "phases"}}]
    for i, tid in enumerate(trace.thread_ids):
        if i:
            events.append({"ph": "M", "pid": pid, "tid": tid, "name": "thread_name",
                           "args": {"name": f"{trace.thread_names.get(i) or 'thread'} {tid}"}})
    for name, a, b in trace.phases:
        events.append({"ph": "X", "pid": pid, "tid": 0, "name": name, "ts": a * 1e6, "dur": (b - a) * 1e6, "cat": "phase"})
    for t, name in trace.marks:
        events.append({"ph": "i", "pid": pid, "tid": 0, "name": name, "ts": t * 1e6, "s": "p"})
    for e in trace.events:
        if e.dur * 1e6 < min_us or e.op in (5, 8):
            continue
        path = trace.files[e.file][0] if e.file < len(trace.files) else "?"
        tid = trace.thread_ids[e.thread] if e.thread < len(trace.thread_ids) else 0
        events.append({"ph": "X", "pid": pid, "tid": tid, "name": f"{OPS.get(e.op)} {path.rsplit(chr(92), 1)[-1]}",
                       "ts": e.t * 1e6, "dur": e.dur * 1e6, "cat": OPS.get(e.op),
                       "args": {"file": path, "size": e.size, "result": e.result,
                                "caller": trace.callers.get(e.stack, ("?",))[0]}})
    previous = None
    for r in trace.counter_rows():
        ts = r["t"] * 1e6
        events.append({"ph": "C", "pid": pid, "name": "memory MB", "ts": ts,
                       "args": {"working set": r.get("workingSet", 0) / 2**20, "available": r.get("availPhys", 0) / 2**20}})
        if previous:
            dt = max(r["t"] - previous["t"], 1e-6)
            events.append({"ph": "C", "pid": pid, "name": "faults/s", "ts": ts,
                           "args": {"process": (r.get("pageFaults", 0) - previous.get("pageFaults", 0)) / dt,
                                    "system hard": ((r.get("sysPageReads", 0) - previous.get("sysPageReads", 0)) % (1 << 32)) / dt}})
            events.append({"ph": "C", "pid": pid, "name": "read MB/s", "ts": ts,
                           "args": {"process": (r.get("readBytes", 0) - previous.get("readBytes", 0)) / dt / 2**20}})
        previous = r
    return {"traceEvents": events, "displayTimeUnit": "ms"}


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def render_text(summary, top=10):
    lines = [f"I/O trace: {summary['events']} events, {summary['dropped']} dropped, hook overhead "
             f"{summary.get('hookOverheadS') or 0:.2f} s"]
    failed = [k for k, v in summary.get("hooks", {}).items() if v != "ok"]
    if failed:
        lines.append("hooks not installed: " + ", ".join(failed))
    for p in summary["phases"]:
        c = p["counters"]
        lines.append("")
        lines.append(f"== {p['name']}  {p['start']:.2f}-{p['end']:.2f} s ({p['seconds']:.2f} s)")
        lines.append(f"  opens {p['opens']} ({p['openS']:.2f} s, {p['failedOpens']} failed)  reads {p['reads']} "
                     f"({p['readS']:.2f} s, {fmt_bytes(p['readBytes'])})  overlapped waits {p['overlappedWaits']} "
                     f"({p['overlappedWaitS']:.2f} s)  map views {p['mapViews']}")
        if c:
            lines.append(f"  page faults {c.get('pageFaults', 0)}  system hard-fault reads {c.get('sysPageReads', 0)} "
                         f"in {c.get('sysPageReadIos', 0)} I/Os  transition {c.get('sysTransition', 0)}  demand-zero "
                         f"{c.get('sysDemandZero', 0)}  process read {fmt_bytes(c.get('readBytes', 0))}")
            if "workingSetStart" in c:
                lines.append(f"  working set {fmt_bytes(c['workingSetStart'])} -> {fmt_bytes(c['workingSetEnd'])}  "
                             f"available {fmt_bytes(c['availPhysStart'])} -> {fmt_bytes(c['availPhysEnd'])} "
                             f"(min {fmt_bytes(c['availPhysMin'])})")
        if p["sizeHistogram"]:
            lines.append("  read sizes: " + "  ".join(f"<={k if k == 'larger' else fmt_bytes(int(k))}: {v[0]} ({v[1]:.2f} s)"
                                                      for k, v in p["sizeHistogram"].items()))
        lines.append("  by kind: " + "  ".join(f"{k} {v.get('readS', 0) + v.get('openS', 0):.2f} s/{v.get('reads', 0)}r"
                                               for k, v in list(p["byKind"].items())[:8]))
        lines.append("  top files (open+read s, reads, bytes, non-sequential, small):")
        for f in p["topFiles"][:top]:
            lines.append(f"    {f['openS'] + f['readS']:7.3f}  {f['reads']:6d}  {fmt_bytes(f['bytes']):>9}  "
                         f"{f['nonSequential']:5d}  {f['smallReads']:5d}  {f['file']}")
        lines.append("  top callers (reads, s, bytes):")
        for c in p["topCallers"][:top]:
            lines.append(f"    {c[1]:6d}  {c[2]:7.3f}  {fmt_bytes(c[3]):>9}  {c[0]}")
        lines.append("  threads by I/O time (tid, name, calls, io s, cpu s, cpu share):")
        for t in p["threads"][:6]:
            lines.append(f"    {t[0]:6d} {t[1][:24]:24} {t[2]:7d} {t[3]:7.2f} {t[4]:7.2f} {t[5]:5.2f}")
        lines.append("  busiest threads by CPU (tid, name, cpu s, share):")
        for t in p["busiestCpuThreads"][:6]:
            lines.append(f"    {t[0]:6d} {t[1][:24]:24} {t[2]:7.2f} {t[3]:5.2f}")
    if summary["dlls"]:
        lines.append("")
        lines.append("DLLs by LoadLibrary + SKSEPlugin_Load time:")
        for d in summary["dlls"][:top * 2]:
            lines.append(f"  {d['loadLibraryS']:7.3f} {d['pluginLoadS']:7.3f}  {d['dll']}")
    if summary["plugins"]:
        rows = sorted(summary["plugins"], key=lambda r: -r["spanS"])
        lines.append("")
        lines.append(f"plugins: {len(rows)}; longest by first-to-last read span (span s, read s, reads, bytes):")
        for r in rows[:top]:
            lines.append(f"  {r['spanS']:7.3f} {r['readS']:7.3f} {r['reads']:6d} {fmt_bytes(r['bytes']):>9}  {r['plugin']}")
    return "\n".join(lines)


def save_outputs(trace, out_dir, title="I/O trace"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = trace.summary()
    paths = {"summary": out / "io-summary.json", "report": out / "io-report.txt", "sqlite": out / "io.sqlite",
             "perfetto": out / "io-perfetto.json.gz"}
    paths["summary"].write_text(json.dumps(summary, indent=1), encoding="utf-8")
    report = f"{title}\n" + render_text(summary)
    paths["report"].write_text(report, encoding="utf-8")
    write_sqlite(trace, paths["sqlite"])
    paths["perfetto"].write_bytes(gzip.compress(json.dumps(perfetto_trace(trace)).encode("utf-8")))
    return paths, report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-io", description=__doc__.splitlines()[0])
    parser.add_argument("dump", help="iotrace-<n>.json (its .bin next to it)")
    parser.add_argument("--out", default=None, help="output directory (default: next to the dump)")
    parser.add_argument("--addrlib", default=None, help="versionlib-1-7-104-0.bin, to name game frames of other runtimes")
    parser.add_argument("--symbols", default=None, help="skyrim-research checkout with the symbol tables")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text()) if args.config else None
    symbols = GameSymbols(research_root(args.symbols, config), address_library_path(args.addrlib, config))
    trace = IoTrace.load(args.dump, symbols)
    out = Path(args.out) if args.out else Path(args.dump).with_suffix("")
    paths, report = save_outputs(trace, out)
    print(json.dumps(trace.summary(), indent=1) if args.json else report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
