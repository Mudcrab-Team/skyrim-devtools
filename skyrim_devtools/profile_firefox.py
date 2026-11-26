"""Firefox Profiler export in the processed profile format, the format `samply` writes.

The file opens at https://profiler.firefox.com via "Load a profile from file" (read in the browser, nothing
is sent anywhere) or with `serve()`, which does what `samply load` does: it serves the file on 127.0.0.1
under a random path and opens profiler.firefox.com/from-url/<that url>. Never press the site's Upload
button: that publishes the profile, game function names included.

The writer targets processed format version 49 (the one samply 0.13 writes); the site upgrades older
versions on load. Counters carry a `display` block so the upgraders keep their graph type and unit.
"""

import gzip
import json
from pathlib import Path
import secrets
import threading
import urllib.parse
import webbrowser

PROCESSED_VERSION = 49
GECKO_VERSION = 24
KIND_NAMES = ["Other", "game", "plugin", "skse", "gpu-driver", "kernel", "system", "other-dll", "unknown"]
KIND_COLORS = ["grey", "blue", "green", "purple", "orange", "red", "yellow", "lightblue", "grey"]
PROFILER_ORIGIN = "https://profiler.firefox.com"

# Marker types the export knows; extra types come in through `marker_schema`.
BASE_SCHEMA = [
    {"name": "Frame", "display": ["marker-chart", "marker-table", "timeline-overview"],
     "chartLabel": "{marker.data.frame}", "tooltipLabel": "Frame {marker.data.frame}",
     "tableLabel": "Frame {marker.data.frame} ({marker.data.ms} ms)",
     "data": [{"key": "frame", "label": "Frame", "format": "integer", "searchable": True},
              {"key": "ms", "label": "Frame time", "format": "milliseconds"}]},
    {"name": "Hitch", "display": ["marker-chart", "marker-table", "timeline-overview"],
     "chartLabel": "Hitch {marker.data.ms} ms", "tooltipLabel": "Hitch: frame {marker.data.frame}",
     "tableLabel": "Hitch: frame {marker.data.frame} ({marker.data.ms} ms)",
     "data": [{"key": "frame", "label": "Frame", "format": "integer", "searchable": True},
              {"key": "ms", "label": "Frame time", "format": "milliseconds"},
              {"key": "waitingPct", "label": "Main thread waiting %", "format": "decimal"},
              {"key": "top", "label": "Top function", "format": "string", "searchable": True}]},
    {"name": "Mark", "display": ["marker-chart", "marker-table", "timeline-overview"],
     "chartLabel": "{marker.data.label}", "tooltipLabel": "{marker.data.kind}: {marker.data.label}",
     "tableLabel": "{marker.data.kind}: {marker.data.label}",
     "data": [{"key": "kind", "label": "Kind", "format": "string", "searchable": True},
              {"key": "label", "label": "Text", "format": "string", "searchable": True}]},
]


class _Strings:
    def __init__(self):
        self.array, self.index = [], {}

    def __call__(self, text):
        if text not in self.index:
            self.index[text] = len(self.array)
            self.array.append(text)
        return self.index[text]


def _table(columns, rows):
    out = {"length": len(rows)}
    for i, name in enumerate(columns):
        out[name] = [row[i] for row in rows]
    return out


class Export:
    """Builds one processed profile. Branch-specific data (GPU timing, board counters, marks) is added with
    `add_markers`, `add_counter` and `marker_schema` before `build()`."""

    def __init__(self, profile, product="SkyrimSE (skyrim-devtools profiler)"):
        self.profile = profile
        self.product = product
        self.schema = [dict(s) for s in BASE_SCHEMA]
        self.counters = []
        self.extra_markers = {}  # thread key ("main" or tid) -> [(name, start, end, data)]
        self.categories = [{"name": n, "color": c, "subcategories": ["Other"]} for n, c in zip(KIND_NAMES, KIND_COLORS)]
        self.idle_category = len(self.categories)
        self.categories.append({"name": "Idle", "color": "transparent", "subcategories": ["Other"]})

    def marker_schema(self, schema):
        if all(s["name"] != schema["name"] for s in self.schema):
            self.schema.append(schema)

    def add_markers(self, markers, thread="main"):
        """markers: (name, startMs, endMs or None, data dict with a `type` matching a schema)."""
        self.extra_markers.setdefault(thread, []).extend(markers)

    def add_counter(self, name, label, times, values, unit="", color="grey", description="", category="GPU"):
        """A gauge (utilisation, clock, temperature) shown as a track: the format stores deltas and draws
        their running sum, so the series is written as differences from a zero start."""
        if not times:
            return
        deltas, previous = [0.0], 0.0
        stamps = [max(times[0] - 0.001, 0.0)]
        for at, value in zip(times, values):
            value = 0.0 if value is None else float(value)
            stamps.append(at)
            deltas.append(value - previous)
            previous = value
        self.counters.append({
            "name": name, "category": category, "description": description or label, "pid": "0", "mainThreadIndex": 0,
            "samples": {"length": len(stamps), "time": stamps, "count": deltas},
            "display": {"graphType": "line-accumulated", "unit": unit, "color": color, "markerSchemaLocation": None,
                        "sortWeight": 60 + len(self.counters), "label": label,
                        "tooltipRows": [{"type": "value", "source": "accumulated",
                                         "format": {"unit": "percent" if unit == "percent" else "number"},
                                         "label": f"{label}{'' if unit in ('', 'percent') else ' (' + unit + ')'}"}]},
        })

    def _base_markers(self, thread):
        profile = self.profile
        out = []
        if not (thread.get("frameThread") or thread.get("main")):
            return out
        hitch_ms = profile.raw.get("hitchThresholdMs", 100.0)
        for start, end, frame in profile.raw.get("frameMarks", []):
            ms = round(end - start, 2)
            out.append(("Frame", start, end, {"type": "Frame", "frame": frame, "ms": ms}))
            if ms > hitch_ms:
                out.append((f"Hitch {ms} ms", start, end, {"type": "Hitch", "frame": frame, "ms": ms}))
        if thread.get("main"):
            known = {m[3].get("frame") for m in out if m[0].startswith("Hitch")}
            for hitch in profile.hitches():
                if hitch.get("frame") in known or hitch.get("startMs") is None:
                    continue
                start = hitch["startMs"]
                top = hitch["topSelf"][0]["function"] if hitch.get("topSelf") else ""
                out.append((f"Hitch {hitch.get('ms')} ms", start, start + (hitch.get("ms") or 0.0),
                            {"type": "Hitch", "frame": hitch.get("frame"), "ms": hitch.get("ms"),
                             "waitingPct": hitch.get("waitingPct"), "top": top}))
            # loads, menus, RPC calls and client Profile.mark spans; a zero-length one is an instant marker
            for mark in profile.raw.get("marks", []):
                if len(mark) < 4 or mark[0] is None:
                    continue
                start, end, kind, text = mark[0], mark[1], str(mark[2]), str(mark[3])
                out.append((f"{kind}: {text}"[:120], start, end if end is not None and end > start else None,
                            {"type": "Mark", "kind": kind, "label": text}))
        return out

    def _thread(self, thread, libs, lib_index):
        profile = self.profile
        strings = _Strings()
        resources, resource_index = [], {}
        funcs, func_index = [], {}
        frames, frame_index = [], {}
        stacks, stack_index = [], {}

        def resource_of(module):
            if module not in resource_index:
                lib = lib_index.get(module)
                resource_index[module] = len(resources)
                resources.append([lib, strings(module), None, 1 if lib is not None else 0])
            return resource_index[module]

        def func_of(function):
            if function not in func_index:
                label, module, _ = profile.functions[function]
                func_index[function] = len(funcs)
                funcs.append([strings(label), False, False, resource_of(module) if module != "?" else None, None, None, None])
            return func_index[function]

        def frame_of(function, idle):
            key = (function, idle)
            if key not in frame_index:
                kind = profile.functions[function][2]
                category = self.idle_category if idle else (KIND_NAMES.index(kind) if kind in KIND_NAMES else 0)
                frame_index[key] = len(frames)
                frames.append([-1, 0, category, 0, func_of(function), None, None, None, None, None])
            return frame_index[key]

        def stack_of(stack, idle):
            prefix = None
            for function in reversed(profile.stack_functions(stack)):
                frame = frame_of(function, idle)
                key = (prefix, frame)
                if key not in stack_index:
                    stack_index[key] = len(stacks)
                    stacks.append([prefix, frame, frames[frame][2], 0])
                prefix = stack_index[key]
            return prefix

        sample_rows = []
        cache = {}
        for at, stack, count, flags in thread.get("runs", []):
            idle = profile.is_wait(stack, flags)
            key = (stack, idle)
            if key not in cache:
                cache[key] = stack_of(stack, idle)
            sample_rows.append([cache[key], round(at, 3), count])

        marker_rows = []
        thread_key = "main" if thread.get("main") else thread["tid"]
        for name, start, end, data in self._base_markers(thread) + self.extra_markers.get(thread_key, []):
            marker_rows.append([0, data, end, strings(name), 1 if end is not None else 0, start])

        name = thread.get("name") or ("Main thread" if thread.get("main") else f"Thread {thread['tid']}")
        end_ms = 1000.0 * profile.raw.get("durationS", 0.0)
        return {
            "name": name, "isMainThread": bool(thread.get("main")), "processType": "default",
            "processName": "SkyrimSE.exe", "pid": "0", "tid": thread["tid"],
            "processStartupTime": 0.0, "processShutdownTime": end_ms, "registerTime": 0.0, "unregisterTime": end_ms,
            "pausedRanges": [], "showMarkersInTimeline": True,
            "samples": dict(_table(["stack", "time", "weight"], sample_rows), weightType="samples"),
            "markers": _table(["category", "data", "endTime", "name", "phase", "startTime"], marker_rows),
            "stackTable": _table(["prefix", "frame", "category", "subcategory"], stacks),
            "frameTable": _table(["address", "inlineDepth", "category", "subcategory", "func", "nativeSymbol",
                                  "innerWindowID", "implementation", "line", "column"], frames),
            "funcTable": _table(["name", "isJS", "relevantForJS", "resource", "fileName", "lineNumber", "columnNumber"], funcs),
            "resourceTable": _table(["lib", "name", "host", "type"], resources),
            "nativeSymbols": {"length": 0, "address": [], "functionSize": [], "libIndex": [], "name": []},
            "stringArray": strings.array,
        }

    def build(self):
        profile = self.profile
        libs, lib_index = [], {}
        for module in profile.modules:
            if module["name"] not in lib_index:
                lib_index[module["name"]] = len(libs)
                libs.append({"name": module["name"], "path": module.get("path", module["name"]), "debugName": module["name"],
                             "debugPath": module.get("path", module["name"]), "breakpadId": "", "codeId": None,
                             "arch": "x86_64"})
        threads = [self._thread(t, libs, lib_index) for t in profile.raw.get("threads", [])]
        threads.sort(key=lambda t: not t["isMainThread"])
        meta = {
            "version": GECKO_VERSION, "preprocessedProfileVersion": PROCESSED_VERSION,
            "interval": 1000.0 / profile.raw.get("hz", 1000.0), "startTime": profile.raw.get("startEpochMs", 0),
            "processType": 0, "product": self.product, "oscpu": "Windows", "platform": "Windows",
            "categories": self.categories, "markerSchema": self.schema, "symbolicated": True, "debug": False,
            "pausedRanges": [], "extensions": {"length": 0, "baseURL": [], "id": [], "name": []},
            "usesOnlyOneStackType": True, "doesNotUseFrameImplementation": True, "sourceCodeIsNotOnSearchfox": True,
        }
        return {"meta": meta, "libs": libs, "threads": threads, "pages": [], "profilerOverhead": [],
                "counters": self.counters}


def write(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
    else:
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return path


def check(data):
    """Structural checks the Firefox Profiler relies on; returns a list of problems (empty when fine)."""
    problems = []
    meta = data.get("meta", {})
    if meta.get("preprocessedProfileVersion") != PROCESSED_VERSION:
        problems.append("meta.preprocessedProfileVersion")
    categories = meta.get("categories") or []
    if not any(c.get("color") == "grey" for c in categories):
        problems.append("no grey default category")
    names = {s["name"] for s in meta.get("markerSchema", [])}
    for t, thread in enumerate(data.get("threads", [])):
        for table in ("samples", "markers", "stackTable", "frameTable", "funcTable", "resourceTable", "nativeSymbols"):
            columns = {k: v for k, v in thread[table].items() if isinstance(v, list)}
            if any(len(v) != thread[table]["length"] for v in columns.values()):
                problems.append(f"thread {t} {table}: column length differs from length")
        strings = len(thread["stringArray"])
        stacks, frames, funcs = thread["stackTable"], thread["frameTable"], thread["funcTable"]
        if any(p is not None and not 0 <= p < i for i, p in enumerate(stacks["prefix"])):
            problems.append(f"thread {t}: stack prefix must point to an earlier stack")
        if any(not 0 <= f < frames["length"] for f in stacks["frame"]):
            problems.append(f"thread {t}: stack frame out of range")
        if any(not 0 <= f < funcs["length"] for f in frames["func"]):
            problems.append(f"thread {t}: frame func out of range")
        if any(not 0 <= c < len(categories) for c in frames["category"]):
            problems.append(f"thread {t}: frame category out of range")
        if any(not 0 <= n < strings for n in funcs["name"]) or any(not 0 <= n < strings for n in thread["markers"]["name"]):
            problems.append(f"thread {t}: string index out of range")
        if any(r is not None and not 0 <= r < thread["resourceTable"]["length"] for r in funcs["resource"]):
            problems.append(f"thread {t}: func resource out of range")
        if any(s is not None and not 0 <= s < stacks["length"] for s in thread["samples"]["stack"]):
            problems.append(f"thread {t}: sample stack out of range")
        times = thread["samples"]["time"]
        if any(b < a for a, b in zip(times, times[1:])):
            problems.append(f"thread {t}: sample times not sorted")
        for data_ in thread["markers"]["data"]:
            if data_ and data_.get("type") not in names:
                problems.append(f"thread {t}: marker type {data_.get('type')} has no schema")
                break
    for counter in data.get("counters", []):
        samples = counter["samples"]
        if len(samples["time"]) != samples["length"] or len(samples["count"]) != samples["length"]:
            problems.append(f"counter {counter['name']}: column length")
    return problems


def from_url(url, origin=PROFILER_ORIGIN):
    return f"{origin}/from-url/{urllib.parse.quote(url, safe='')}/"


def serve(path, open_browser=True, port=0, origin=PROFILER_ORIGIN):
    """Serves one profile file on 127.0.0.1 under a random path, the way `samply load` does, and opens the
    profiler on it. The browser fetches the file from this machine; nothing is uploaded. Returns the server
    (call shutdown() to stop) and the profiler URL."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    path = Path(path)
    token = secrets.token_urlsafe(16)
    name = "profile.json"
    body = path.read_bytes()
    gzipped = path.suffix == ".gz"

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            # Chrome's private network access check for a public page reading 127.0.0.1.
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self):  # noqa: N802 - http.server naming
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers", "*"))
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != f"/{token}/{name}":
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=UTF-8")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{token}/{name}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profiler_url = from_url(url, origin)
    if open_browser:
        webbrowser.open(profiler_url)
    return server, profiler_url
