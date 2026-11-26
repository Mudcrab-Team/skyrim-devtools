"""Live and timeline views over the sampling profiler (SPDX-License-Identifier: GPL-3.0-or-later).

`top_snapshot` turns one Profile.top reply (plus Perf.frameStats) into a symbolised dashboard row set;
`render_top` draws it for a terminal. `timeline_html` writes a self-contained viewer of a stopped profile:
one row per thread with its samples as a flame strip, presented frames as bands, and marker spans.
"""

from collections import Counter
import json
from pathlib import Path
import time

from . import profiling
from .profile_gpu import BOUND_LABELS, classify

KIND_ORDER = ("load", "hitch", "alert", "narrate", "tool", "rpc", "menu", "marker")


def _mb(value):
    if value is None:
        return "-"
    return f"{value / 1024:.2f} GB" if value >= 1024 else f"{value:.0f} MB"


def _rows(profile, items, samples, count_source=False):
    """Fold per-pc counts from Profile.top into per-function rows, largest first."""
    totals = Counter()
    for frame in items or []:
        index = profile._function(frame, count_source=count_source)
        totals[index] += frame.get("n", 0)
    rows = []
    for index, count in totals.most_common():
        label, module, kind = profile.functions[index]
        rows.append({"function": label, "module": module, "kind": kind, "samples": count,
                     "pct": round(100.0 * count / samples, 1) if samples else 0.0})
    return rows


async def top_snapshot(call, symbols=None, seconds=3.0, top=15):
    """One dashboard snapshot. `call(method, **params)` is any RPC caller."""
    began = time.monotonic()
    raw = await call("Profile.top", seconds=seconds)
    rpc_ms = (time.monotonic() - began) * 1000.0
    try:
        stats = await call("Perf.frameStats", window=seconds)
    except Exception:  # noqa: BLE001 - the Present hook may be absent (headless) and the rest still reads
        stats = {}
    profile = profiling.Profile({"modules": raw.get("modules", []), "frames": [], "stacks": []}, symbols)
    samples = raw.get("samples", 0)
    waiting = raw.get("waiting", 0)
    on_cpu = samples - waiting
    self_rows = _rows(profile, raw.get("self"), samples, count_source=True)
    total_rows = {r["function"]: r["pct"] for r in _rows(profile, raw.get("total"), samples)}
    for row in self_rows:
        row["totalPct"] = total_rows.get(row["function"])
    named = sum(r["samples"] for r in self_rows if r["kind"] == "game" and not r["function"].startswith(("sub_", "id:", "game+0x")))
    game = sum(r["samples"] for r in self_rows if r["kind"] == "game")
    modules = Counter()
    for row in self_rows:
        modules[row["kind"]] += row["samples"]
    snapshot = {
        "at": time.strftime("%H:%M:%S"),
        "windowS": raw.get("seconds", seconds), "hz": raw.get("hz"), "ready": raw.get("ready", False),
        "warm": raw.get("warm", False), "profiling": raw.get("profiling", False),
        "fps": stats.get("fps"), "p50Ms": stats.get("p50Ms"), "p99Ms": stats.get("p99Ms"), "maxMs": stats.get("maxMs"),
        "frames": stats.get("count"),
        "main": {"samples": samples, "busyPct": round(100.0 * on_cpu / samples, 1) if samples else None,
                 "waitingPct": round(100.0 * waiting / samples, 1) if samples else None,
                 "namedPct": round(100.0 * named / game, 1) if game else None,
                 "modules": {k: round(100.0 * v / samples, 1) for k, v in modules.most_common()} if samples else {}},
        "topSelf": self_rows[:top],
        "topTotal": sorted(({"function": f, "pct": p} for f, p in total_rows.items()), key=lambda r: -r["pct"])[:top],
        "waits": _rows(profile, raw.get("waits"), samples)[:5],
        "memory": raw.get("memory"), "threads": raw.get("threads"), "gpu": raw.get("gpu"),
        "costMs": {**(raw.get("costMs") or {}), "resolve": raw.get("resolveMs"), "rpc": round(rpc_ms, 1),
                   "resolverRebuilt": bool(raw.get("resolverRebuilt"))},
        "gpuTiming": _gpu_timing(raw.get("gpuTiming"), stats.get("p50Ms"), on_cpu / samples if samples else None,
                                 (raw.get("gpu") or {}).get("utilPct")),
    }
    credits = profile.credits()
    if credits:
        snapshot["credits"] = credits
    return snapshot


def _gpu_timing(timing, frame_ms, busy_share, util_pct=None):
    """GPU frame time next to the main thread's sampled busy time per frame, and which one limits the frame.
    The live summary has no per-frame CPU issue span, so the utilisation reading is what tells a capped game
    from a GPU-bound one here."""

    if not timing:
        return None
    out = dict(timing)
    gpu_ms = timing.get("gpuMsP50")
    cpu_ms = round(frame_ms * busy_share, 2) if frame_ms and busy_share is not None else None
    out["cpuMsP50"] = cpu_ms
    if gpu_ms is not None and cpu_ms is not None and frame_ms:
        out["bound"] = classify(frame_ms, cpu_ms, gpu_ms, timing.get("excessMsP50"), util_pct)
    return out


def _bar(pct, width=20):
    if pct is None:
        return " " * width
    filled = int(round(min(max(pct, 0.0), 100.0) / 100.0 * width))
    return "#" * filled + "." * (width - filled)


def render_top(snap, width=110):
    out = []
    state = "profiling" if snap["profiling"] else ("live" if snap["warm"] else "warming up")
    out.append(f"skyrim-devtools-profile top  {snap['at']}  last {snap['windowS']:g} s, main thread at {snap['hz']:g} Hz  [{state}]")
    fps = snap.get("fps")
    out.append(f"fps {fps if fps is not None else '-':>6}   frame p50 {snap.get('p50Ms') or '-'} ms   p99 {snap.get('p99Ms') or '-'} ms"
               f"   max {snap.get('maxMs') or '-'} ms   frames {snap.get('frames') or '-'}")
    main = snap["main"]
    out.append(f"main busy  [{_bar(main['busyPct'])}] {main['busyPct'] if main['busyPct'] is not None else '-'}%   "
               f"waiting {main['waitingPct'] if main['waitingPct'] is not None else '-'}%   samples {main['samples']}   "
               f"game names {main['namedPct'] if main['namedPct'] is not None else '-'}%")
    if main["modules"]:
        out.append("main split " + "  ".join(f"{k} {v}%" for k, v in main["modules"].items()))
    memory = snap.get("memory") or {}
    if memory:
        faults = memory.get("pageFaultsPerS")
        out.append(f"memory     working set {_mb(memory.get('workingSetMb'))}   private {_mb(memory.get('privateMb'))}   "
                   f"resident {memory.get('residentPct')}%   faults {faults if faults is not None else '-'}/s   "
                   f"system free {_mb(memory.get('systemAvailableMb'))}")
    threads = snap.get("threads") or {}
    gpu = snap.get("gpu") or {}
    line = []
    if threads:
        cpu = threads.get("processCpuPct")
        line.append(f"process CPU {cpu if cpu is not None else '-'}% of {threads.get('cores')} cores, {threads.get('count')} threads")
    if gpu:
        engines = " ".join(f"{k} {v}%" for k, v in (gpu.get("engines") or {}).items())
        line.append(f"GPU {gpu.get('utilPct') if gpu.get('utilPct') is not None else '-'}%"
                    + (f" ({engines})" if engines else "") + f"   VRAM {_mb(gpu.get('vramMb'))}")
    if line:
        out.append("   ".join(line))
    timing = snap.get("gpuTiming") or {}
    if timing.get("frames"):
        passes = ", ".join(f"{p['pass']} {p['msPerFrame']}" for p in (timing.get("passes") or [])[:4])
        out.append(f"GPU frame  p50 {timing.get('gpuMsP50')} ms  p95 {timing.get('gpuMsP95')} ms   main CPU ~{timing.get('cpuMsP50')} ms"
                   f"   bound: {BOUND_LABELS.get(timing.get('bound'), '-')}   passes ms: {passes}")
    elif timing:
        out.append(f"GPU frame  not timed ({timing.get('reason') or 'no frames yet'})")
    out.append("")
    out.append(f"{'self%':>6} {'total%':>6}  {'function':<60} module")
    for row in snap["topSelf"]:
        total = row.get("totalPct")
        name = row["function"] if len(row["function"]) <= 60 else row["function"][:59] + "~"
        out.append(f"{row['pct']:6.1f} {total if total is not None else '':>6}  {name:<60} {row['module']}")
    if not snap["topSelf"]:
        out.append("   (no main-thread samples yet)" if not snap["ready"] else "   (main thread only waited)")
    if snap["waits"]:
        out.append("waiting in: " + "; ".join(f"{r['function']} {r['pct']}%" for r in snap["waits"][:3]))
    if threads.get("top"):
        out.append("")
        out.append(f"{'cpu%':>6}  thread")
        for row in threads["top"][:8]:
            pct = row.get("pct")
            out.append(f"{pct if pct is not None else '-':>6}  {row['tid']:>6} {row.get('name') or ''}")
    if snap.get("credits"):
        out.append("")
        out.extend(snap["credits"])
    return "\n".join(line[:width] if len(line) > width else line for line in out)


# ---- marks ---------------------------------------------------------------------------------------


def recorder_marks(path, start_epoch_ms, duration_ms):
    """Tool calls, narration and alerts from a skyrim-use timeline file, as profile marks
    [startMs, endMs, kind, name] relative to the profile start."""
    marks = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return marks
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        kind = record.get("t")
        if kind == "tool":
            start, end, name = record.get("startMs"), record.get("endMs"), record.get("name", "?")
        elif kind in ("narrate", "alert"):
            start = end = record.get("ms")
            name = record.get("text") or record.get("message") or kind
        else:
            continue
        if start is None:
            continue
        start -= start_epoch_ms
        end = (end if end is not None else record.get("startMs")) - start_epoch_ms
        if end < 0 or start > duration_ms:
            continue
        marks.append([round(start, 2), round(end, 2), kind, str(name)[:200]])
    return marks


# ---- timeline -------------------------------------------------------------------------------------


def timeline_data(profile):
    """The profile reduced to what the viewer draws: function labels, a function-level stack tree, per-thread
    samples and the spans."""
    raw = profile.raw
    func_tree = {}
    tree = []

    def node(stack):
        # map a sample stack to a prefix-tree node over functions (root first)
        if stack in func_tree:
            return func_tree[stack]
        functions = profile.stack_functions(stack)
        parent = -1
        for function in reversed(functions):
            key = (parent, function)
            index = tree_index.get(key)
            if index is None:
                index = tree_index[key] = len(tree)
                tree.append([parent, function])
            parent = index
        func_tree[stack] = parent
        return parent

    tree_index = {}
    threads = []
    for thread in raw.get("threads", []):
        samples = []
        on_cpu = 0
        for at, stack, count, flags in thread.get("runs", []):
            waiting = 1 if profile.is_wait(stack, flags) else 0
            samples.append([at, node(stack), count, waiting])
            on_cpu += 0 if waiting else count
        if not samples:
            continue
        threads.append({"tid": thread.get("tid"), "name": thread.get("name") or "", "main": thread.get("main", False),
                        "frameThread": thread.get("frameThread", False), "onCpu": on_cpu, "samples": samples})
    threads.sort(key=lambda t: (not t["main"], not t["frameThread"], -t["onCpu"]))
    marks = [list(m) for m in raw.get("marks", [])]
    for hitch in raw.get("hitches", []):
        marks.append([hitch.get("startMs"), round(hitch.get("startMs", 0) + hitch.get("ms", 0), 2), "hitch",
                      f"frame {hitch.get('frame')}: {hitch.get('ms')} ms"])
    marks.sort(key=lambda m: (m[0] if m[0] is not None else 0))
    context = raw.get("context") or {}
    from .profile_gpu import gpu_frames

    timed, offset = gpu_frames(raw)
    pass_names, pass_index = [], {}
    gpu_rows = []
    for f in timed:
        passes = []
        for label, kind, start, end in f["passes"]:
            key = (label, kind)
            if key not in pass_index:
                pass_index[key] = len(pass_names)
                pass_names.append([label, kind])
            passes.append([start, end, pass_index[key]])
        gpu_rows.append([f["gpuStart"], f["gpuEnd"], f["frame"], passes])
    return {
        "gpu": {"frames": gpu_rows, "passes": pass_names, "offsetMs": offset,
                "reason": None if gpu_rows else (raw.get("gpu") or {}).get("reason")},
        "title": context.get("phase") or "profile",
        "durationMs": round(raw.get("durationS", 0) * 1000.0, 2),
        "periodMs": 1000.0 / raw.get("hz", 1000.0),
        "hz": raw.get("hz"),
        "hitchMs": raw.get("hitchThresholdMs", 100),
        "functions": [[label, module, kind] for label, module, kind in profile.functions],
        "tree": tree,
        "threads": threads,
        "frames": raw.get("frameMarks", []),
        "marks": marks,
        "credits": profile.credits(),
        "context": context,
    }


def timeline_html(profile, title="Skyrim SE profile"):
    data = json.dumps(timeline_data(profile), separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", title.replace("<", "&lt;")).replace("__DATA__", data)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #fbfbfa; --panel: #ffffff; --ink: #1d1f22; --muted: #6b7078; --line: #e2e3e6;
  --band-a: #eef0f3; --band-b: #e3e6eb; --band-slow: #f0b46a; --band-hitch: #e0574b; --sel: #2f6fdf;
  --wait: #c9ccd2; --cross: #1d1f22;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --panel: #1c1f23; --ink: #e6e7e9; --muted: #969ba3; --line: #2c3036;
    --band-a: #23272c; --band-b: #2b3036; --band-slow: #9a6a2c; --band-hitch: #d4554a; --sel: #6d9cf0;
    --wait: #3a3f46; --cross: #e6e7e9;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 8px 16px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid var(--line); }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header .meta { color: var(--muted); }
header label { color: var(--muted); }
header button { font: inherit; font-size: 12px; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: 4px; padding: 1px 8px; cursor: pointer; min-width: 28px; }
header button:disabled { color: var(--muted); cursor: default; }
header, #view { user-select: none; -webkit-user-select: none; }
#wrap { display: flex; height: calc(100vh - 44px); }
#view { flex: 1; position: relative; overflow-y: auto; overflow-x: hidden; }
#r { position: sticky; top: 0; z-index: 2; display: block; cursor: col-resize; }
#c { display: block; cursor: crosshair; }
#o { position: absolute; left: 0; pointer-events: none; z-index: 1; }
#side { width: 380px; max-width: 45vw; border-left: 1px solid var(--line); background: var(--panel); overflow: auto; padding: 10px 14px; }
#side h2 { font-size: 13px; margin: 12px 0 4px; }
#side table { width: 100%; border-collapse: collapse; font-size: 12px; }
#side td { padding: 1px 4px; vertical-align: top; }
#side td.n { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }
#side .stack { font-size: 11px; color: var(--muted); margin: 2px 0 8px; word-break: break-word; }
#tip { position: fixed; pointer-events: none; background: var(--panel); border: 1px solid var(--line); padding: 4px 8px;
  font-size: 12px; max-width: 520px; display: none; z-index: 5; box-shadow: 0 2px 8px rgba(0,0,0,.15); }
#tip b { font-variant-numeric: tabular-nums; }
#help { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: none; align-items: center; justify-content: center; z-index: 10; }
#help.open { display: flex; }
#help .card { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 18px; max-width: min(520px, calc(100vw - 32px)); }
#help h2 { font-size: 14px; margin: 0 0 8px; }
#help td { padding: 2px 10px 2px 0; vertical-align: top; }
#help kbd { font: 11px ui-monospace, monospace; border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; background: var(--bg); }
.legend span { display: inline-block; width: 10px; height: 10px; margin: 0 3px 0 8px; vertical-align: -1px; border-radius: 2px; }
.hint { color: var(--muted); font-size: 12px; }
@media (max-width: 700px) { #wrap { flex-direction: column; height: auto; } #side { width: auto; max-width: none; border-left: 0; } }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <span class="meta" id="meta"></span>
  <span class="legend" id="legend"></span>
  <label><input type="checkbox" id="idle"> idle threads</label>
  <span>
    <button id="zout" title="Zoom out (-)">&minus;</button>
    <button id="back" title="Previous zoom (Backspace)">Back</button>
    <button id="fit" title="Fit all (0)">Fit</button>
    <button id="helpbtn" title="Keys (?)">?</button>
  </span>
  <span class="hint" id="span"></span>
</header>
<div id="wrap">
  <div id="view"><canvas id="r"></canvas><canvas id="c"></canvas><canvas id="o"></canvas></div>
  <div id="side"></div>
</div>
<div id="tip"></div>
<div id="help"><div class="card">
  <h2>Timeline keys</h2>
  <table>
    <tr><td><kbd>drag</kbd></td><td>select a time range on the timeline or ruler and zoom to it</td></tr>
    <tr><td><kbd>Alt</kbd>/<kbd>Shift</kbd> drag, middle drag</td><td>pan</td></tr>
    <tr><td><kbd>wheel</kbd></td><td>zoom around the pointer; <kbd>Shift</kbd>+wheel or horizontal scroll pans</td></tr>
    <tr><td><kbd>click</kbd></td><td>frame stacks in the side panel (a thread row focuses that thread)</td></tr>
    <tr><td><kbd>Backspace</kbd></td><td>back to the previous zoom</td></tr>
    <tr><td><kbd>-</kbd> <kbd>+</kbd></td><td>zoom out / in</td></tr>
    <tr><td><kbd>0</kbd> <kbd>f</kbd>, double-click</td><td>fit the whole profile</td></tr>
    <tr><td><kbd>&larr;</kbd> <kbd>&rarr;</kbd></td><td>pan by a fifth of the view</td></tr>
    <tr><td><kbd>Esc</kbd></td><td>clear the selection, cancel a drag, close this help</td></tr>
    <tr><td><kbd>?</kbd></td><td>show or hide this help</td></tr>
  </table>
  <p class="hint" id="framekey"></p>
</div></div>
<script>
const D = __DATA__;
const KIND_COLORS = { game: [28, 80], plugin: [212, 65], skse: [270, 50], "gpu-driver": [130, 45], kernel: [0, 60],
  system: [185, 45], "other-dll": [45, 25], unknown: [0, 0] };
const MARK_COLORS = { load: "#3a8fd8", hitch: "#d9443a", alert: "#e08b1a", narrate: "#7d5bd0", tool: "#2aa198",
  rpc: "#8a94a6", menu: "#b5a33a", marker: "#c45ab3" };
const MARK_ORDER = ["load", "hitch", "alert", "narrate", "tool", "rpc", "menu", "marker"];
const PASS_COLORS = { shadows: "#6b7fa8", main: "#3f9d6b", scene: "#55b37f", post: "#c98b3a", offscreen: "#9a7bc0",
  frame: "#8a94a6", present: "#d0d4dc", other: "#a0a0a0" };
const GPU = D.gpu || { frames: [], passes: [] };
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
const ruler = document.getElementById("r"), rctx = ruler.getContext("2d");
const over = document.getElementById("o"), octx = over.getContext("2d");
const view = document.getElementById("view"), side = document.getElementById("side"), tip = document.getElementById("tip");
const help = document.getElementById("help");
const LABEL_W = 150, AXIS_H = 20, FRAME_H = 18, LANE_H = 12, LEVEL_H = 5, MAX_LEVELS = 14;
// below about 50 us a 1 kHz profile has nothing left to separate, and float labels start to repeat
const MIN_SPAN = 0.05;
const SIDE_HINT = '<p class="hint">Click a frame band or a thread row to see that frame\'s stacks. Press ? for keys.</p>';
side.innerHTML = SIDE_HINT;

document.getElementById("title").textContent = "Timeline: " + D.title;
document.getElementById("meta").textContent = `${(D.durationMs / 1000).toFixed(1)} s at ${D.hz} Hz, ${D.threads.length} threads, ${D.frames.length} frames`;
document.getElementById("legend").innerHTML = Object.entries(KIND_COLORS).filter(([k]) => k !== "unknown")
  .map(([k, [h, s]]) => `<span style="background:hsl(${h} ${s}% 55%)"></span>${k}`).join("");

const funcs = D.functions, tree = D.tree;
const depthCache = new Int16Array(tree.length).fill(-1);
function depth(n) { if (n < 0) return 0; if (depthCache[n] >= 0) return depthCache[n];
  const d = depth(tree[n][0]) + 1; depthCache[n] = d; return d; }
function path(n) { const out = []; while (n >= 0) { out.push(tree[n][1]); n = tree[n][0]; } return out.reverse(); }
const colorCache = new Map();
function funcColor(f) {
  let c = colorCache.get(f); if (c) return c;
  const [label, , kind] = funcs[f]; const [h, s] = KIND_COLORS[kind] || KIND_COLORS.unknown;
  let hash = 0; for (let i = 0; i < label.length; i++) hash = (hash * 31 + label.charCodeAt(i)) | 0;
  c = `hsl(${h + (hash % 14)} ${s}% ${45 + Math.abs(hash % 22)}%)`; colorCache.set(f, c); return c;
}

// expand runs into per-sample arrays for binary search
for (const t of D.threads) {
  let n = 0; for (const s of t.samples) n += s[2];
  t.t = new Float64Array(n); t.node = new Int32Array(n); t.wait = new Uint8Array(n);
  let i = 0;
  for (const [at, node, count, waiting] of t.samples) for (let k = 0; k < count; k++) {
    t.t[i] = at + k * D.periodMs; t.node[i] = node; t.wait[i] = waiting; i++; }
  t.maxDepth = 0; for (let k = 0; k < n; k++) t.maxDepth = Math.max(t.maxDepth, depth(t.node[k]));
  t.levels = Math.max(2, Math.min(MAX_LEVELS, t.maxDepth));
}
function lower(arr, x) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; }

// a frame is slow relative to this run's own pace, so a 30 fps capture is not painted all orange
const frameEnds = D.frames.map(f => f[1]);
const frameLens = D.frames.map(f => f[1] - f[0]).sort((a, b) => a - b);
const MEDIAN_FRAME = frameLens.length ? frameLens[frameLens.length >> 1] : 16.7;
const SLOW_MS = MEDIAN_FRAME * 1.5;
document.getElementById("framekey").textContent = D.frames.length
  ? `Frame boxes: orange above ${SLOW_MS.toFixed(1)} ms (1.5x the median ${MEDIAN_FRAME.toFixed(1)} ms), red above the ${D.hitchMs} ms hitch threshold.` : "";

// marks into lanes per kind (greedy, at most 3 lanes per kind)
const lanes = [];
for (const kind of MARK_ORDER) {
  const ms = D.marks.filter(m => m[2] === kind); if (!ms.length) continue;
  const ends = [], rows = [];
  for (const m of ms) { let l = ends.findIndex(e => e <= m[0]); if (l < 0) { if (ends.length < 3) { l = ends.length; ends.push(0); rows.push([]); } else l = ends.length - 1; }
    ends[l] = Math.max(ends[l], m[1]); rows[l].push(m); }
  rows.forEach((r, i) => lanes.push({ kind, label: i ? "" : kind, marks: r }));
}

const FULL = Math.max(D.durationMs, 1);
let t0 = 0, t1 = FULL, selected = null, showIdle = false, layout = [], P = {};
let hover = null, range = null, drag = null, wheelAt = 0;
const zoomHistory = [];
function visibleThreads() { return D.threads.filter(t => showIdle || t.onCpu > 0 || t.main); }
function W() { return view.clientWidth; }
function x(t) { return LABEL_W + (t - t0) / (t1 - t0) * (W() - LABEL_W); }
function tAt(px) { return t0 + (px - LABEL_W) / (W() - LABEL_W) * (t1 - t0); }

// ---- time formatting: the ruler switches from whole seconds to ms to us offsets as the view narrows
function decimals(step) { return Math.max(0, Math.ceil(-Math.log10(step) - 1e-9)); }
function fmtDur(ms) { const a = Math.abs(ms);
  if (a >= 1000) return (ms / 1000).toFixed(a >= 10000 ? 1 : 2) + " s";
  if (a >= 1) return ms.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2) + " ms";
  return (ms * 1000).toFixed(a >= 0.01 ? 0 : 1) + " µs"; }
function fmtAbs(ms, dec) { return (ms / 1000).toFixed(dec) + " s"; }
function fmtOffset(ms, step) {
  if (step < 1 && ms < 1) return "+" + (ms * 1000).toFixed(decimals(step * 1000)) + " µs";
  return "+" + ms.toFixed(step < 1 ? 3 : 0) + " ms"; }
function ticks() {
  const target = (t1 - t0) * 90 / Math.max(W() - LABEL_W, 1), p = Math.pow(10, Math.floor(Math.log10(target)));
  const step = Math.max(0.001, [1, 2, 5, 10].map(m => m * p).find(s => s >= target * (1 - 1e-9)));
  const first = Math.ceil(t0 / step - 1e-9), at = [];
  for (let i = first; i * step <= t1 && at.length < 400; i++) at.push(i * step);
  let labels;
  if (step >= 1000) labels = at.map(g => fmtAbs(g, 0));
  else if (step >= 10) labels = at.map(g => fmtAbs(g, decimals(step / 1000)));
  else labels = at.map((g, i) => i ? fmtOffset(g - at[0], step) : fmtAbs(g, decimals(step / 1000)));
  return { step, at, labels };
}
function fmtCursor(time) {
  const perPx = (t1 - t0) / Math.max(W() - LABEL_W, 1);
  return fmtAbs(time, Math.min(7, Math.max(3, decimals(perPx / 1000))));
}

// ---- zoom state
function setView(a, b, remember) {
  let span = Math.min(Math.max(b - a, MIN_SPAN), FULL * 1.2);
  const mid = (a + b) / 2; a = mid - span / 2;
  if (remember && (Math.abs(a - t0) > 1e-9 || Math.abs(span - (t1 - t0)) > 1e-9)) {
    zoomHistory.push([t0, t1]); if (zoomHistory.length > 200) zoomHistory.shift(); }
  t0 = a; t1 = a + span; draw();
}
function zoomBy(k, at) { if (at === undefined) at = (t0 + t1) / 2; const f = (at - t0) / (t1 - t0), span = (t1 - t0) * k;
  setView(at - f * span, at - f * span + span, true); }
function panBy(dt) { t0 += dt; t1 += dt; draw(); }
function fit() { setView(0, FULL, true); }
function back() { const v = zoomHistory.pop(); if (v) { t0 = v[0]; t1 = v[1]; draw(); } }

function palette() { P = { bg: css("--bg"), ink: css("--ink"), muted: css("--muted"), line: css("--line"), a: css("--band-a"),
  b: css("--band-b"), slow: css("--band-slow"), hitch: css("--band-hitch"), sel: css("--sel"), wait: css("--wait"), cross: css("--cross") }; }

function draw() {
  palette();
  const threads = visibleThreads();
  layout = [];
  let y = 4;
  layout.push({ type: "frames", y, h: FRAME_H }); y += FRAME_H + 4;
  for (const lane of lanes) { layout.push({ type: "lane", lane, y, h: LANE_H }); y += LANE_H + 2; }
  y += 4;
  for (const t of threads) { const h = t.levels * LEVEL_H + 4; layout.push({ type: "thread", thread: t, y, h }); y += h + 3; }
  if (GPU.frames.length) { y += 4; layout.push({ type: "gpu", y, h: FRAME_H }); y += FRAME_H + 2;
    layout.push({ type: "passes", y, h: FRAME_H }); y += FRAME_H + 4; }
  const dpr = window.devicePixelRatio || 1, w = W(), h = Math.max(y + 10, view.clientHeight - AXIS_H);
  for (const [cv, ch] of [[canvas, h], [over, h], [ruler, AXIS_H]]) {
    cv.width = w * dpr; cv.height = ch * dpr; cv.style.width = w + "px"; cv.style.height = ch + "px"; }
  over.style.top = AXIS_H + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); octx.setTransform(dpr, 0, 0, dpr, 0, 0); rctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = P.bg; ctx.fillRect(0, 0, w, h);
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle";
  ctx.strokeStyle = P.line;
  for (const g of ticks().at) { const gx = Math.round(x(g)) + 0.5; ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, h); ctx.stroke(); }
  for (const row of layout) {
    ctx.fillStyle = P.muted;
    if (row.type === "frames") { ctx.fillText("frames", 6, row.y + row.h / 2); drawFrames(row); }
    else if (row.type === "lane") { ctx.fillText(row.lane.label, 6, row.y + row.h / 2); drawLane(row); }
    else if (row.type === "gpu") { ctx.fillStyle = P.ink; ctx.fillText("GPU frames", 6, row.y + row.h / 2); drawGpu(row, false); }
    else if (row.type === "passes") { ctx.fillText("GPU passes", 6, row.y + row.h / 2); drawGpu(row, true); }
    else { const t = row.thread; ctx.fillStyle = P.ink;
      ctx.fillText((t.main ? "main thread" : (t.name || "thread") + (t.frameThread ? " (presents)" : "")).slice(0, 22), 6, row.y + 7);
      ctx.fillStyle = P.muted; ctx.fillText(`${t.tid}  ${(t.onCpu * D.periodMs / 1000).toFixed(2)} s cpu`, 6, row.y + 19);
      drawThread(row); }
  }
  if (selected) { ctx.fillStyle = P.sel; ctx.globalAlpha = 0.15;
    ctx.fillRect(x(selected.start), 0, Math.max(1, x(selected.end) - x(selected.start)), h); ctx.globalAlpha = 1; }
  document.getElementById("span").textContent = `view ${fmtDur(t1 - t0)}`;
  document.getElementById("back").disabled = !zoomHistory.length;
  drawOverlay();
}
function drawFrames(row) {
  const w = W();
  ctx.save(); ctx.font = "10px system-ui, sans-serif";
  for (let i = lower(frameEnds, t0); i < D.frames.length && D.frames[i][0] <= t1; i++) {
    const [s, e, n] = D.frames[i], ms = e - s, hitch = ms > D.hitchMs, slow = ms > SLOW_MS;
    const xs = Math.max(LABEL_W, x(s)), xe = Math.min(w, x(e)); if (xe < LABEL_W) continue;
    const bw = Math.max(1, xe - xs - (xe - xs > 3 ? 1 : 0));
    ctx.fillStyle = hitch ? P.hitch : slow ? P.slow : (n % 2 ? P.a : P.b);
    ctx.fillRect(xs, row.y, bw, row.h);
    const label = bw > 74 ? `#${n}  ${ms.toFixed(1)} ms` : bw > 30 ? ms.toFixed(1) : "";
    if (label) { ctx.save(); ctx.beginPath(); ctx.rect(xs, row.y, bw, row.h); ctx.clip();
      ctx.fillStyle = hitch ? "#fff" : P.ink; ctx.fillText(label, xs + 3, row.y + row.h / 2); ctx.restore(); }
  }
  if (selected && selected.frame !== null) { ctx.strokeStyle = P.sel; ctx.lineWidth = 2;
    ctx.strokeRect(x(selected.start) + 1, row.y + 1, Math.max(2, x(selected.end) - x(selected.start) - 2), row.h - 2); }
  ctx.restore();
}
function gpuFirst() { return lower(GPU.frames.map(f => f[1]), t0); }
function drawGpu(row, passes) {
  for (let i = gpuFirst(); i < GPU.frames.length && GPU.frames[i][0] <= t1; i++) {
    const [s, e, n, ps] = GPU.frames[i];
    if (!passes) { ctx.fillStyle = n % 2 ? P.a : P.b;
      const xs = Math.max(LABEL_W, x(s)), xe = x(e); ctx.fillRect(xs, row.y, Math.max(1, xe - xs), row.h); continue; }
    for (const [ps0, pe, pi] of ps) { const xs = Math.max(LABEL_W, x(ps0)), xe = x(pe); if (xe < LABEL_W || xe - xs < 0.3) continue;
      ctx.fillStyle = PASS_COLORS[GPU.passes[pi][1]] || PASS_COLORS.other; ctx.fillRect(xs, row.y, Math.max(1, xe - xs - (xe - xs > 3 ? 1 : 0)), row.h);
      if (xe - xs > 60) { ctx.save(); ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.rect(xs, row.y, xe - xs, row.h); ctx.clip();
        ctx.fillText(GPU.passes[pi][1] + " " + GPU.passes[pi][0], xs + 3, row.y + row.h / 2); ctx.restore(); } }
  }
}
function gpuAt(time) { for (let i = gpuFirst(); i < GPU.frames.length && GPU.frames[i][0] <= t1; i++) {
  const f = GPU.frames[i]; if (f[0] <= time && time <= f[1]) return f; } return null; }
function drawLane(row) {
  ctx.fillStyle = MARK_COLORS[row.lane.kind] || "#888";
  for (const m of row.lane.marks) { if (m[1] < t0 || m[0] > t1) continue;
    const xs = Math.max(LABEL_W, x(m[0])), xe = Math.max(xs + 2, x(m[1])); ctx.fillRect(xs, row.y, xe - xs, row.h);
    if (xe - xs > 40) { ctx.save(); ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.rect(xs, row.y, xe - xs, row.h); ctx.clip();
      ctx.fillText(m[3], xs + 3, row.y + row.h / 2); ctx.restore(); } }
}
function sampleAt(t, time) { const i = lower(t.t, time); return i < t.t.length && t.t[i] - time <= D.periodMs * 1.5 ? i : -1; }
function drawThread(row) {
  const t = row.thread, w = W(), wait = P.wait;
  const cols = new Int32Array(w).fill(-1);
  for (let px = LABEL_W; px < w; px++) cols[px] = sampleAt(t, tAt(px));
  for (let level = 0; level < t.levels; level++) {
    let px = LABEL_W;
    while (px < w) {
      const s = cols[px]; if (s < 0) { px++; continue; }
      const p = path(t.node[s]); const f = level < p.length ? p[level] : -1; const waiting = t.wait[s];
      let end = px + 1;
      while (end < w && cols[end] >= 0) { const q = path(t.node[cols[end]]);
        if ((level < q.length ? q[level] : -1) !== f || t.wait[cols[end]] !== waiting) break; end++; }
      if (f >= 0) { ctx.fillStyle = waiting ? wait : funcColor(f); ctx.fillRect(px, row.y + 2 + level * LEVEL_H, end - px, LEVEL_H - 1); }
      px = end;
    }
  }
}
// path() walks are repeated per level; memoise the last few
const pathCache = new Map(); const rawPath = path;
path = n => { let p = pathCache.get(n); if (!p) { p = rawPath(n); if (pathCache.size > 50000) pathCache.clear(); pathCache.set(n, p); } return p; };

// ---- ruler, crosshair and range overlay: redrawn on every pointer move, so they never repaint the sample rows
function drawRuler() {
  const w = W(); rctx.fillStyle = P.bg; rctx.fillRect(0, 0, w, AXIS_H);
  rctx.font = "11px system-ui, sans-serif"; rctx.textBaseline = "middle";
  rctx.strokeStyle = P.line; rctx.beginPath(); rctx.moveTo(0, AXIS_H - 0.5); rctx.lineTo(w, AXIS_H - 0.5); rctx.stroke();
  const { at, labels } = ticks();
  rctx.fillStyle = P.muted;
  at.forEach((g, i) => { const gx = Math.round(x(g)) + 0.5; if (gx < LABEL_W) return;
    rctx.beginPath(); rctx.moveTo(gx, AXIS_H - 6); rctx.lineTo(gx, AXIS_H); rctx.stroke(); rctx.fillText(labels[i], gx + 3, 9); });
  rctx.fillStyle = P.bg; rctx.fillRect(0, 0, LABEL_W, AXIS_H);
  rctx.fillStyle = P.muted; rctx.fillText(at.length && (at[1] - at[0]) < 10 ? "time (offsets)" : "time", 6, 9);
  if (range) { const a = Math.max(LABEL_W, x(Math.min(range.a, range.b))), b = x(Math.max(range.a, range.b));
    rctx.fillStyle = P.sel; rctx.globalAlpha = 0.3; rctx.fillRect(a, 0, Math.max(1, b - a), AXIS_H); rctx.globalAlpha = 1; }
  if (hover && hover.x > LABEL_W) { const label = fmtCursor(tAt(hover.x));
    rctx.font = "bold 11px system-ui, sans-serif"; const tw = rctx.measureText(label).width + 8;
    const bx = Math.min(hover.x - tw / 2, w - tw); rctx.fillStyle = P.cross; rctx.fillRect(Math.max(LABEL_W, bx), 1, tw, AXIS_H - 3);
    rctx.fillStyle = P.bg; rctx.fillText(label, Math.max(LABEL_W, bx) + 4, 9); }
}
function pill(c, text, px, py, color) {
  c.font = "10px system-ui, sans-serif"; const tw = Math.min(c.measureText(text).width, 260) + 6, w = W();
  const bx = px + 6 + tw > w ? px - 6 - tw : px + 6;
  c.fillStyle = P.bg; c.globalAlpha = 0.85; c.fillRect(bx, py - 6, tw, 12); c.globalAlpha = 1;
  c.save(); c.beginPath(); c.rect(bx, py - 6, tw, 12); c.clip();
  c.fillStyle = color || P.ink; c.textBaseline = "middle"; c.fillText(text, bx + 3, py); c.restore();
}
function drawOverlay() {
  const w = W(), h = over.height / (window.devicePixelRatio || 1);
  octx.clearRect(0, 0, w, h);
  const top = view.scrollTop, bottom = top + view.clientHeight - AXIS_H;
  if (range) {
    const lo = Math.min(range.a, range.b), hi = Math.max(range.a, range.b);
    const a = Math.max(LABEL_W, x(lo)), b = Math.min(w, x(hi));
    octx.fillStyle = P.sel; octx.globalAlpha = 0.18; octx.fillRect(a, 0, Math.max(1, b - a), h); octx.globalAlpha = 1;
    octx.strokeStyle = P.sel; octx.beginPath(); octx.moveTo(a + 0.5, 0); octx.lineTo(a + 0.5, h); octx.moveTo(b - 0.5, 0); octx.lineTo(b - 0.5, h); octx.stroke();
    octx.font = "bold 12px system-ui, sans-serif"; const text = fmtDur(hi - lo), tw = octx.measureText(text).width + 12;
    const cx = Math.min(Math.max((a + b) / 2 - tw / 2, LABEL_W), w - tw);
    octx.fillStyle = P.sel; octx.fillRect(cx, top + 6, tw, 18);
    octx.fillStyle = "#fff"; octx.textBaseline = "middle"; octx.fillText(text, cx + 6, top + 15);
  }
  if (hover && hover.x > LABEL_W && !(drag && drag.mode === "pan")) {
    const px = Math.round(hover.x) + 0.5, time = tAt(hover.x);
    octx.strokeStyle = P.cross; octx.globalAlpha = 0.6; octx.beginPath(); octx.moveTo(px, 0); octx.lineTo(px, h); octx.stroke(); octx.globalAlpha = 1;
    for (const row of layout) { if (row.y + row.h < top || row.y > bottom) continue;
      const v = rowValue(row, time); if (v) pill(octx, v.text, px, row.y + Math.min(row.h / 2, 8), v.color); }
  }
  drawRuler();
}

// ---- what sits under the pointer
function frameAt(time) {
  const i = lower(frameEnds, time); const f = D.frames[i];
  if (f && f[0] <= time) return { start: f[0], end: f[1], frame: f[2] };
  return { start: time - 8, end: time + 8, frame: null };
}
function markAt(lane, time) { const slack = 2 * (t1 - t0) / W(); return lane.marks.find(m => m[0] <= time + slack && time <= m[1] + slack); }
// one short value per row for the crosshair; rows without a value at this time stay quiet
function rowValue(row, time) {
  if (row.type === "frames") { const f = frameAt(time); if (f.frame === null) return null; const ms = f.end - f.start;
    return { text: `#${f.frame}  ${ms.toFixed(1)} ms`, color: ms > D.hitchMs ? P.hitch : null }; }
  if (row.type === "lane") { const m = markAt(row.lane, time); return m ? { text: m[3] } : null; }
  if (row.type === "gpu" || row.type === "passes") { const f = gpuAt(time); return f ? { text: `${(f[1] - f[0]).toFixed(1)} ms` } : null; }
  if (row.type === "thread") { const t = row.thread, s = sampleAt(t, time); if (s < 0) return null; const p = path(t.node[s]);
    if (!p.length) return null; return { text: (t.wait[s] ? "waiting: " : "") + funcs[p[p.length - 1]][0], color: t.wait[s] ? P.muted : null }; }
  return null;
}
// the longer text for the row the pointer is on
function rowDetail(row, time, y) {
  if (row.type === "frames") { const f = frameAt(time); return f.frame !== null ? `frame ${f.frame}: ${(f.end - f.start).toFixed(2)} ms` : ""; }
  if (row.type === "lane") { const m = markAt(row.lane, time); return m ? `${m[2]}: ${m[3]} (${fmtDur(m[1] - m[0])})` : ""; }
  if (row.type === "gpu" || row.type === "passes") { const f = gpuAt(time);
    if (!f) return "";
    const p = row.type === "passes" ? f[3].find(q => q[0] <= time && time <= q[1]) : null;
    return p ? `${GPU.passes[p[2]][1]}: ${GPU.passes[p[2]][0]} ${(p[1] - p[0]).toFixed(3)} ms (frame ${f[2]})`
             : `GPU frame ${f[2]}: ${(f[1] - f[0]).toFixed(2)} ms (alignment estimated)`; }
  if (row.type === "thread") { const t = row.thread, s = sampleAt(t, time); if (s < 0) return ""; const p = path(t.node[s]);
    const level = Math.floor((y - row.y - 2) / LEVEL_H), f = p[Math.min(level, p.length - 1)];
    return f === undefined ? "" : (t.wait[s] ? "[waiting] " : "") + `${funcs[f][0]}  (${funcs[f][1]}, depth ${Math.min(level, p.length - 1)} of ${p.length})`; }
  return "";
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]); }
function showSelection(threadFocus) {
  const { start, end, frame } = selected;
  let html = `<h2>${frame !== null ? "Frame " + frame : "Window"}: ${(end - start).toFixed(1)} ms at ${(start / 1000).toFixed(3)} s</h2>`;
  const marks = D.marks.filter(m => m[1] >= start && m[0] <= end);
  if (marks.length) html += "<table>" + marks.slice(0, 12).map(m => `<tr><td class="n">${esc(m[2])}</td><td>${esc(m[3])}</td><td class="n">${(m[1] - m[0]).toFixed(1)} ms</td></tr>`).join("") + "</table>";
  const gf = frame !== null ? GPU.frames.find(f => f[2] === frame) : null;
  if (gf) { const sums = new Map(); for (const [s, e, pi] of gf[3]) sums.set(pi, (sums.get(pi) || 0) + e - s);
    html += `<h2>GPU: ${(gf[1] - gf[0]).toFixed(2)} ms</h2><table>` + [...sums].sort((p, q) => q[1] - p[1]).slice(0, 8).map(([pi, ms]) =>
      `<tr><td class="n">${ms.toFixed(2)} ms</td><td>${esc(GPU.passes[pi][1])} <span class="hint">${esc(GPU.passes[pi][0])}</span></td></tr>`).join("") + "</table>"; }
  const threads = threadFocus ? [threadFocus] : visibleThreads().slice(0, 3);
  for (const t of threads) {
    const a = lower(t.t, start), b = lower(t.t, end); const self = new Map(), stacks = new Map(); let waiting = 0;
    for (let i = a; i < b; i++) { if (t.wait[i]) { waiting++; continue; } const p = path(t.node[i]); if (!p.length) continue;
      const leaf = p[p.length - 1]; self.set(leaf, (self.get(leaf) || 0) + 1); stacks.set(t.node[i], (stacks.get(t.node[i]) || 0) + 1); }
    const n = b - a; if (!n) continue;
    html += `<h2>${esc(t.main ? "main thread" : t.name || t.tid)}: ${n} samples, ${Math.round(100 * waiting / n)}% waiting</h2><table>`;
    html += [...self].sort((p, q) => q[1] - p[1]).slice(0, 10).map(([f, c]) =>
      `<tr><td class="n">${(100 * c / n).toFixed(0)}%</td><td>${esc(funcs[f][0])} <span class="hint">${esc(funcs[f][1])}</span></td></tr>`).join("") + "</table>";
    for (const [node, c] of [...stacks].sort((p, q) => q[1] - p[1]).slice(0, 3))
      html += `<div class="stack"><b>${(100 * c / n).toFixed(0)}%</b> ` + path(node).map(f => esc(funcs[f][0])).join(" &rsaquo; ") + "</div>";
  }
  if (D.credits && D.credits.length) html += `<p class="hint">${D.credits.map(esc).join("<br>")}</p>`;
  side.innerHTML = html;
}
function clearSelection() { selected = null; side.innerHTML = SIDE_HINT; draw(); }
function rowAt(y) { return layout.find(r => y >= r.y && y < r.y + r.h + 2); }

// ---- pointer: plain drag selects a range and zooms to it; Alt/Shift/middle drag pans
function localX(e) { return e.clientX - canvas.getBoundingClientRect().left; }
function localY(e) { return e.clientY - canvas.getBoundingClientRect().top; }
function startDrag(e, onRuler) {
  const px = localX(e); if (px <= LABEL_W && e.button === 0) return;
  e.preventDefault();
  const pan = e.button === 1 || e.altKey || e.shiftKey;
  drag = { mode: pan ? "pan" : "range", x: px, y: localY(e), t0, t1, time: tAt(px), moved: false, onRuler, remembered: false };
}
canvas.addEventListener("mousedown", e => startDrag(e, false));
ruler.addEventListener("mousedown", e => startDrag(e, true));
window.addEventListener("mousemove", e => {
  const px = localX(e), py = localY(e);
  if (drag) {
    if (Math.abs(px - drag.x) > 3) drag.moved = true;
    hover = { x: px, y: py }; tip.style.display = "none";
    if (drag.mode === "pan") { if (!drag.moved) return;
      if (!drag.remembered) { zoomHistory.push([drag.t0, drag.t1]); drag.remembered = true; }
      const dt = (px - drag.x) / (W() - LABEL_W) * (drag.t1 - drag.t0); t0 = drag.t0 - dt; t1 = drag.t1 - dt; draw(); }
    else if (drag.moved) { range = { a: drag.time, b: tAt(Math.max(LABEL_W, Math.min(W(), px))) }; drawOverlay(); }
    return;
  }
  const inside = e.target === canvas || e.target === ruler;
  if (!inside) { if (hover) { hover = null; tip.style.display = "none"; drawOverlay(); } return; }
  hover = { x: px, y: py }; drawOverlay();
  const row = e.target === canvas ? rowAt(py) : null, time = tAt(px);
  if (px <= LABEL_W) { tip.style.display = "none"; return; }
  const detail = row ? rowDetail(row, time, py) : "";
  let html = `<b>${esc(fmtCursor(time))}</b>`;
  if (selected) html += ` <span class="hint">${esc((time >= selected.start ? "+" : "") + fmtDur(time - selected.start))} from selection</span>`;
  if (detail) html += "<br>" + esc(detail);
  tip.innerHTML = html; tip.style.display = "block";
  tip.style.left = Math.min(e.clientX + 12, window.innerWidth - 530) + "px"; tip.style.top = e.clientY + 14 + "px";
});
window.addEventListener("mouseup", e => {
  if (!drag) return;
  const d = drag; drag = null;
  if (d.mode === "range") {
    if (d.moved && range) { const lo = Math.min(range.a, range.b), hi = Math.max(range.a, range.b); range = null; setView(lo, hi, true); return; }
    range = null;
    if (!d.moved && !d.onRuler && e.target === canvas && d.x > LABEL_W) {
      const row = rowAt(d.y); selected = frameAt(tAt(d.x));
      showSelection(row && row.type === "thread" ? row.thread : null); draw(); return; }
    drawOverlay();
  }
});
canvas.addEventListener("mouseleave", () => { if (!drag) { hover = null; tip.style.display = "none"; drawOverlay(); } });
ruler.addEventListener("mouseleave", () => { if (!drag) { hover = null; tip.style.display = "none"; drawOverlay(); } });
for (const target of [canvas, ruler]) target.addEventListener("wheel", e => {
  e.preventDefault(); const px = localX(e);
  if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY; panBy(d / (W() - LABEL_W) * (t1 - t0)); return; }
  // one history entry per wheel gesture, not per notch
  const now = performance.now(), fresh = now - wheelAt > 400; wheelAt = now;
  const at = tAt(Math.max(px, LABEL_W)), k = Math.exp(e.deltaY * 0.0015), f = (at - t0) / (t1 - t0), span = (t1 - t0) * k;
  setView(at - f * span, at - f * span + span, fresh);
}, { passive: false });
view.addEventListener("scroll", () => drawOverlay());
canvas.addEventListener("dblclick", fit);
window.addEventListener("keydown", e => {
  if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey) return;
  const at = hover && hover.x > LABEL_W ? tAt(hover.x) : undefined;
  if (e.key === "Escape") {
    if (help.classList.contains("open")) help.classList.remove("open");
    else if (drag) { drag = null; range = null; drawOverlay(); }
    else clearSelection();
  }
  else if (e.key === "?") help.classList.toggle("open");
  else if (e.key === "Backspace") { e.preventDefault(); back(); }
  else if (e.key === "+" || e.key === "=") zoomBy(0.5, at);
  else if (e.key === "-" || e.key === "_") zoomBy(2, at);
  else if (e.key === "0" || e.key === "f") fit();
  else if (e.key === "ArrowLeft") { e.preventDefault(); panBy(-(t1 - t0) / 5); }
  else if (e.key === "ArrowRight") { e.preventDefault(); panBy((t1 - t0) / 5); }
});
help.addEventListener("click", e => { if (e.target === help) help.classList.remove("open"); });
document.getElementById("zout").addEventListener("click", () => zoomBy(2));
document.getElementById("back").addEventListener("click", back);
document.getElementById("fit").addEventListener("click", fit);
document.getElementById("helpbtn").addEventListener("click", () => help.classList.toggle("open"));
document.getElementById("idle").addEventListener("change", e => { showIdle = e.target.checked; draw(); });
window.addEventListener("resize", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
draw();
</script>
</body>
</html>
"""
