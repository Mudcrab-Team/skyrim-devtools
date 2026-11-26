"""A profile as a normalised SQLite database, in the spirit of Nsight Systems' export, and read-only queries.

Times are milliseconds from the profile's start on the game's QPC clock. Strings live once in StringIds;
the *V views join them back in for hand-written queries. See docs/profiling.md, SQLite export.
"""

import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

from .profile_gpu import counter_series, frame_table, gpu_frames

SCHEMA = """
CREATE TABLE Meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE StringIds (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
CREATE TABLE ThreadNames (threadId INTEGER PRIMARY KEY, nameId INTEGER, isMain INTEGER, isFrameThread INTEGER,
    onCpuSamples INTEGER, waitingSamples INTEGER);
CREATE TABLE Frames (frame INTEGER PRIMARY KEY, start REAL, end REAL, frame_ms REAL, cpu_ms REAL, gpu_ms REAL,
    gpu_start REAL, gpu_end REAL, hitch INTEGER, bound TEXT, excess_ms REAL, gpu_busy_ms REAL, util_pct REAL);
CREATE TABLE Symbols (id INTEGER PRIMARY KEY, rva INTEGER, nameId INTEGER, moduleId INTEGER, kind TEXT, sourceId INTEGER);
CREATE TABLE Callchains (id INTEGER, depth INTEGER, symbolId INTEGER, moduleId INTEGER, PRIMARY KEY (id, depth));
CREATE TABLE CpuSamples (timestamp REAL, threadId INTEGER, callchainId INTEGER, state TEXT, weight INTEGER, end REAL);
CREATE TABLE Markers (id INTEGER PRIMARY KEY, kind TEXT, start REAL, end REAL, textId INTEGER);
CREATE TABLE GpuRanges (frame INTEGER, passId INTEGER, kind TEXT, start REAL, end REAL, ms REAL);
CREATE TABLE Counters (timestamp REAL, nameId INTEGER, value REAL);
CREATE TABLE GpaCounters (sample INTEGER, labelId INTEGER, nameId INTEGER, groupId INTEGER, usage TEXT, value REAL);
CREATE INDEX CpuSamplesTime ON CpuSamples (timestamp);
CREATE INDEX CpuSamplesThread ON CpuSamples (threadId, timestamp);
CREATE INDEX CallchainsSymbol ON Callchains (symbolId, depth);
CREATE INDEX GpuRangesFrame ON GpuRanges (frame);
CREATE VIEW SymbolsV AS SELECT s.id, s.rva, n.value AS name, m.value AS module, s.kind, src.value AS source
    FROM Symbols s JOIN StringIds n ON n.id = s.nameId JOIN StringIds m ON m.id = s.moduleId
    JOIN StringIds src ON src.id = s.sourceId;
CREATE VIEW ThreadsV AS SELECT t.threadId, n.value AS name, t.isMain, t.isFrameThread, t.onCpuSamples, t.waitingSamples
    FROM ThreadNames t JOIN StringIds n ON n.id = t.nameId;
CREATE VIEW MarkersV AS SELECT k.id, k.kind, k.start, k.end, k.end - k.start AS ms, t.value AS text
    FROM Markers k JOIN StringIds t ON t.id = k.textId;
CREATE VIEW GpuRangesV AS SELECT g.frame, p.value AS pass, g.kind, g.start, g.end, g.ms
    FROM GpuRanges g JOIN StringIds p ON p.id = g.passId;
CREATE VIEW GpaCountersV AS SELECT g.sample, l.value AS label, n.value AS name, gr.value AS grp, g.usage, g.value
    FROM GpaCounters g JOIN StringIds l ON l.id = g.labelId JOIN StringIds n ON n.id = g.nameId
    JOIN StringIds gr ON gr.id = g.groupId;
CREATE VIEW CountersV AS SELECT c.timestamp, n.value AS name, c.value FROM Counters c JOIN StringIds n ON n.id = c.nameId;
CREATE VIEW LeafSamplesV AS SELECT c.timestamp, c.end, c.threadId, c.state, c.weight, s.name, s.module, s.kind
    FROM CpuSamples c JOIN Callchains cc ON cc.id = c.callchainId AND cc.depth = 0 JOIN SymbolsV s ON s.id = cc.symbolId;
"""


def export_sqlite(profile, path):
    """Write the profile to `path` (replaced if present); returns row counts per table."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.unlink(missing_ok=True)
    raw = profile.raw
    db = sqlite3.connect(temp)
    try:
        db.executescript(SCHEMA)
        strings = {}

        def sid(text):
            text = "" if text is None else str(text)
            found = strings.get(text)
            if found is None:
                found = strings[text] = len(strings) + 1
            return found

        period = 1000.0 / raw.get("hz", 1000.0)
        samples, used, thread_rows = [], set(), []
        for thread in raw.get("threads", []):
            on = off = 0
            for at, stack, count, flags in thread.get("runs", []):
                waiting = profile.is_wait(stack, flags)
                samples.append((at, thread["tid"], stack, "waiting" if waiting else "running", count, round(at + count * period, 3)))
                used.add(stack)
                if waiting:
                    off += count
                else:
                    on += count
            name = thread.get("name") or ("main" if thread.get("main") else "")
            thread_rows.append((thread["tid"], sid(name), int(bool(thread.get("main"))), int(bool(thread.get("frameThread"))), on, off))
        chains = []
        for stack in sorted(used):
            for depth, function in enumerate(profile.stack_functions(stack)):
                chains.append((stack, depth, function, sid(profile.functions[function][1])))
        meta = profile.function_meta
        symbols = [(i, meta[i].get("rva"), sid(label), sid(module), kind, sid(meta[i].get("source")))
                   for i, (label, module, kind) in enumerate(profile.functions)]
        marks = [list(m) for m in raw.get("marks", [])]
        for hitch in raw.get("hitches", []):
            if hitch.get("startMs") is not None:
                marks.append([hitch["startMs"], round(hitch["startMs"] + hitch.get("ms", 0), 3), "hitch",
                              f"frame {hitch.get('frame')}: {hitch.get('ms')} ms"])
        markers = [(i, m[2], m[0], m[1], sid(m[3])) for i, m in enumerate(sorted(marks, key=lambda m: m[0] or 0))]
        frames = [(r["frame"], r["start"], r["end"], r["frameMs"], r["cpuMs"], r["gpuMs"], r["gpuStart"], r["gpuEnd"],
                   int(r["hitch"]), r["bound"], r["excessMs"], r["gpuBusyMs"], r["utilPct"]) for r in frame_table(profile)]
        timed, offset = gpu_frames(raw)
        ranges = [(f["frame"], sid(label), kind, start, end, round(end - start, 3))
                  for f in timed for label, kind, start, end in f["passes"]]
        counters = [(at, sid(name), value) for at, name, value in counter_series(raw)]
        gpa = raw.get("gpaCounters") or {}
        labels = gpa.get("samples") or []
        gpa_rows = [(i, sid(labels[i] if i < len(labels) else ""), sid(c.get("name")), sid(c.get("group")), c.get("usage"), v)
                    for c in gpa.get("counters") or [] for i, v in enumerate(c.get("values") or [])
                    if isinstance(v, (int, float))] if gpa.get("ok") else []
        board = (raw.get("gpuMetrics") or {})
        context = raw.get("context") or {}
        meta_rows = {
            "format": "skyrim-devtools-profile-sqlite", "version": "1", "pluginBuild": raw.get("pluginBuild"),
            "hz": raw.get("hz"), "samplePeriodMs": period, "durationS": raw.get("durationS"),
            "startEpochMs": raw.get("startEpochMs"), "threadsSpec": raw.get("threadsSpec"),
            "hitchThresholdMs": raw.get("hitchThresholdMs"), "symbolSources": json.dumps(dict(profile.sources)),
            "credits": json.dumps(profile.credits()), "gpuAlignmentOffsetMs": offset,
            "gpuAlignment": "GPU times are placed as early as the CPU issue times allow (docs/profiling.md, GPU timing)",
            "gpuAvailable": (raw.get("gpu") or {}).get("available"), "context": json.dumps(context) if context else None,
            "timeUnit": "ms from profile start (QPC clock)",
            "gpuBoardSource": board.get("source"), "gpuBoardName": board.get("gpu"),
            "gpaDevice": gpa.get("device"), "gpaPassCount": gpa.get("passCount"), "gpaFramesUsed": gpa.get("framesUsed"),
            "gpaOverhead": json.dumps(gpa["overhead"]) if gpa.get("overhead") else None,
            "gpaError": None if not gpa or gpa.get("ok") else gpa.get("error"),
        }
        db.executemany("INSERT INTO StringIds VALUES (?, ?)", ((i, t) for t, i in strings.items()))
        db.executemany("INSERT INTO ThreadNames VALUES (?, ?, ?, ?, ?, ?)", thread_rows)
        db.executemany("INSERT INTO Frames VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", frames)
        db.executemany("INSERT INTO Symbols VALUES (?, ?, ?, ?, ?, ?)", symbols)
        db.executemany("INSERT INTO Callchains VALUES (?, ?, ?, ?)", chains)
        db.executemany("INSERT INTO CpuSamples VALUES (?, ?, ?, ?, ?, ?)", samples)
        db.executemany("INSERT INTO Markers VALUES (?, ?, ?, ?, ?)", markers)
        db.executemany("INSERT INTO GpuRanges VALUES (?, ?, ?, ?, ?, ?)", ranges)
        db.executemany("INSERT INTO Counters VALUES (?, ?, ?)", counters)
        db.executemany("INSERT INTO GpaCounters VALUES (?, ?, ?, ?, ?, ?)", gpa_rows)
        db.executemany("INSERT INTO Meta VALUES (?, ?)", ((k, None if v is None else str(v)) for k, v in meta_rows.items()))
        db.commit()
    finally:
        db.close()
    temp.replace(path)
    return {"Frames": len(frames), "CpuSamples": len(samples), "Callchains": len(chains), "Symbols": len(symbols),
            "Markers": len(markers), "GpuRanges": len(ranges), "Counters": len(counters), "GpaCounters": len(gpa_rows), "ThreadNames": len(thread_rows),
            "StringIds": len(strings)}


# Named blocks let checks pick a query by name; a plain list of blocks shifts whenever a recipe is added.
_DOC_QUERY = re.compile(r"<!-- query: ([\w-]+) -->\s*```sql\n(.*?)```", re.S)


def documented_queries(text):
    """{name: sql} of the SQL blocks in a markdown text that carry a `<!-- query: NAME -->` line."""
    return {name: sql.strip().rstrip(";") for name, sql in _DOC_QUERY.findall(text)}


_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _authorize(action, *_):
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def query(path, sql, limit=200, timeout_s=10.0):
    """Run one read-only statement against an exported profile; returns columns, rows and whether rows
    were cut at `limit`. Writes, ATTACH and PRAGMA are refused."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"no profile database at {path}")
    db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only = ON")
        db.set_authorizer(_authorize)
        deadline = time.monotonic() + timeout_s
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        cursor = db.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max(1, limit) + 1)
        truncated = len(rows) > limit
        return {"columns": columns, "rows": [list(r) for r in rows[:limit]], "truncated": truncated, "path": str(path)}
    finally:
        db.close()
