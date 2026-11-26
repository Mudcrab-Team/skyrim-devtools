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
    raw = await call("Profile.top", seconds=seconds)
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
    }
    credits = profile.credits()
    if credits:
        snapshot["credits"] = credits
    return snapshot


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
    return {
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
  --band-a: #eef0f3; --band-b: #e3e6eb; --band-slow: #f4c7c3; --band-hitch: #e0574b; --sel: #2f6fdf;
  --wait: #c9ccd2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --panel: #1c1f23; --ink: #e6e7e9; --muted: #969ba3; --line: #2c3036;
    --band-a: #23272c; --band-b: #2b3036; --band-slow: #6b3a36; --band-hitch: #d4554a; --sel: #6d9cf0;
    --wait: #3a3f46;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 10px 16px; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; border-bottom: 1px solid var(--line); }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header .meta { color: var(--muted); }
header label { color: var(--muted); }
#wrap { display: flex; height: calc(100vh - 44px); }
#view { flex: 1; position: relative; overflow-y: auto; overflow-x: hidden; }
canvas { display: block; cursor: crosshair; }
#side { width: 380px; max-width: 45vw; border-left: 1px solid var(--line); background: var(--panel); overflow: auto; padding: 10px 14px; }
#side h2 { font-size: 13px; margin: 12px 0 4px; }
#side table { width: 100%; border-collapse: collapse; font-size: 12px; }
#side td { padding: 1px 4px; vertical-align: top; }
#side td.n { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }
#side .stack { font-size: 11px; color: var(--muted); margin: 2px 0 8px; word-break: break-word; }
#tip { position: fixed; pointer-events: none; background: var(--panel); border: 1px solid var(--line); padding: 4px 8px;
  font-size: 12px; max-width: 520px; display: none; z-index: 5; box-shadow: 0 2px 8px rgba(0,0,0,.15); }
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
  <span class="hint">wheel: zoom, drag: pan, click: frame stacks, double-click: reset</span>
</header>
<div id="wrap">
  <div id="view"><canvas id="c"></canvas></div>
  <div id="side"><p class="hint">Click a frame band or a thread row to see that frame's stacks.</p></div>
</div>
<div id="tip"></div>
<script>
const D = __DATA__;
const KIND_COLORS = { game: [28, 80], plugin: [212, 65], skse: [270, 50], "gpu-driver": [130, 45], kernel: [0, 60],
  system: [185, 45], "other-dll": [45, 25], unknown: [0, 0] };
const MARK_COLORS = { load: "#3a8fd8", hitch: "#d9443a", alert: "#e08b1a", narrate: "#7d5bd0", tool: "#2aa198",
  rpc: "#8a94a6", menu: "#b5a33a", marker: "#c45ab3" };
const MARK_ORDER = ["load", "hitch", "alert", "narrate", "tool", "rpc", "menu", "marker"];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
const view = document.getElementById("view"), side = document.getElementById("side"), tip = document.getElementById("tip");
const LABEL_W = 150, AXIS_H = 20, FRAME_H = 16, LANE_H = 12, LEVEL_H = 5, MAX_LEVELS = 14;

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

// marks into lanes per kind (greedy, at most 3 lanes per kind)
const lanes = [];
for (const kind of MARK_ORDER) {
  const ms = D.marks.filter(m => m[2] === kind); if (!ms.length) continue;
  const ends = [], rows = [];
  for (const m of ms) { let l = ends.findIndex(e => e <= m[0]); if (l < 0) { if (ends.length < 3) { l = ends.length; ends.push(0); rows.push([]); } else l = ends.length - 1; }
    ends[l] = Math.max(ends[l], m[1]); rows[l].push(m); }
  rows.forEach((r, i) => lanes.push({ kind, label: i ? "" : kind, marks: r }));
}

let t0 = 0, t1 = Math.max(D.durationMs, 1), selected = null, showIdle = false, layout = [];
function visibleThreads() { return D.threads.filter(t => showIdle || t.onCpu > 0 || t.main); }
function W() { return view.clientWidth; }
function x(t) { return LABEL_W + (t - t0) / (t1 - t0) * (W() - LABEL_W); }
function tAt(px) { return t0 + (px - LABEL_W) / (W() - LABEL_W) * (t1 - t0); }

function draw() {
  const threads = visibleThreads();
  layout = [];
  let y = AXIS_H;
  layout.push({ type: "frames", y, h: FRAME_H }); y += FRAME_H + 4;
  for (const lane of lanes) { layout.push({ type: "lane", lane, y, h: LANE_H }); y += LANE_H + 2; }
  y += 4;
  for (const t of threads) { const h = t.levels * LEVEL_H + 4; layout.push({ type: "thread", thread: t, y, h }); y += h + 3; }
  const dpr = window.devicePixelRatio || 1, w = W(), h = Math.max(y + 10, view.clientHeight);
  canvas.width = w * dpr; canvas.height = h * dpr; canvas.style.width = w + "px"; canvas.style.height = h + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, w, h);
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle";
  // axis
  const span = t1 - t0, step = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 60000].find(s => span / s < (w - LABEL_W) / 90) || 60000;
  ctx.fillStyle = css("--muted"); ctx.strokeStyle = css("--line");
  for (let g = Math.ceil(t0 / step) * step; g <= t1; g += step) { const gx = x(g);
    ctx.fillText(g >= 1000 ? (g / 1000).toFixed(step < 1000 ? 2 : 0) + " s" : g + " ms", gx + 2, 10);
    ctx.beginPath(); ctx.moveTo(gx, AXIS_H - 4); ctx.lineTo(gx, h); ctx.stroke(); }
  for (const row of layout) {
    ctx.fillStyle = css("--muted");
    if (row.type === "frames") { ctx.fillText("frames", 6, row.y + row.h / 2); drawFrames(row); }
    else if (row.type === "lane") { ctx.fillText(row.lane.label, 6, row.y + row.h / 2); drawLane(row); }
    else { const t = row.thread; ctx.fillStyle = css("--ink");
      ctx.fillText((t.main ? "main thread" : (t.name || "thread") + (t.frameThread ? " (presents)" : "")).slice(0, 22), 6, row.y + 7);
      ctx.fillStyle = css("--muted"); ctx.fillText(`${t.tid}  ${(t.onCpu * D.periodMs / 1000).toFixed(2)} s cpu`, 6, row.y + 19);
      drawThread(row); }
  }
  if (selected) { ctx.fillStyle = css("--sel"); ctx.globalAlpha = 0.15;
    ctx.fillRect(x(selected.start), AXIS_H, Math.max(1, x(selected.end) - x(selected.start)), h); ctx.globalAlpha = 1; }
}
function drawFrames(row) {
  const a = lower(D.frames.map(f => f[1]), t0);
  for (let i = a; i < D.frames.length && D.frames[i][0] <= t1; i++) {
    const [s, e, n] = D.frames[i], ms = e - s;
    ctx.fillStyle = ms > D.hitchMs ? css("--band-hitch") : ms > 33.4 ? css("--band-slow") : (n % 2 ? css("--band-a") : css("--band-b"));
    const xs = Math.max(LABEL_W, x(s)), xe = x(e); if (xe < LABEL_W) continue;
    ctx.fillRect(xs, row.y, Math.max(1, xe - xs - (xe - xs > 3 ? 1 : 0)), row.h);
  }
}
function drawLane(row) {
  ctx.fillStyle = MARK_COLORS[row.lane.kind] || "#888";
  for (const m of row.lane.marks) { if (m[1] < t0 || m[0] > t1) continue;
    const xs = Math.max(LABEL_W, x(m[0])), xe = Math.max(xs + 2, x(m[1])); ctx.fillRect(xs, row.y, xe - xs, row.h);
    if (xe - xs > 40) { ctx.save(); ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.rect(xs, row.y, xe - xs, row.h); ctx.clip();
      ctx.fillText(m[3], xs + 3, row.y + row.h / 2); ctx.restore(); } }
}
function sampleAt(t, time) { const i = lower(t.t, time); return i < t.t.length && t.t[i] - time <= D.periodMs * 1.5 ? i : -1; }
function drawThread(row) {
  const t = row.thread, w = W(), wait = css("--wait");
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

function frameAt(time) {
  for (const f of D.frames) if (f[0] <= time && time <= f[1]) return { start: f[0], end: f[1], frame: f[2] };
  return { start: time - 8, end: time + 8, frame: null };
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]); }
function showSelection(threadFocus) {
  const { start, end, frame } = selected;
  let html = `<h2>${frame !== null ? "Frame " + frame : "Window"}: ${(end - start).toFixed(1)} ms at ${(start / 1000).toFixed(3)} s</h2>`;
  const marks = D.marks.filter(m => m[1] >= start && m[0] <= end);
  if (marks.length) html += "<table>" + marks.slice(0, 12).map(m => `<tr><td class="n">${esc(m[2])}</td><td>${esc(m[3])}</td><td class="n">${(m[1] - m[0]).toFixed(1)} ms</td></tr>`).join("") + "</table>";
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
function rowAt(y) { return layout.find(r => y >= r.y && y < r.y + r.h + 2); }

let drag = null;
canvas.addEventListener("mousedown", e => { drag = { x: e.offsetX, t0, t1, moved: false }; });
window.addEventListener("mouseup", e => {
  if (drag && !drag.moved && e.target === canvas && e.offsetX > LABEL_W) {
    const row = rowAt(e.offsetY); selected = frameAt(tAt(e.offsetX));
    showSelection(row && row.type === "thread" ? row.thread : null); draw();
  }
  drag = null;
});
canvas.addEventListener("mousemove", e => {
  if (drag) { const dx = e.offsetX - drag.x; if (Math.abs(dx) > 2) drag.moved = true;
    const dt = dx / (W() - LABEL_W) * (drag.t1 - drag.t0); t0 = drag.t0 - dt; t1 = drag.t1 - dt; draw(); tip.style.display = "none"; return; }
  const row = rowAt(e.offsetY), time = tAt(e.offsetX); let text = "";
  if (row && e.offsetX > LABEL_W) {
    if (row.type === "frames") { const f = frameAt(time); if (f.frame !== null) text = `frame ${f.frame}: ${(f.end - f.start).toFixed(1)} ms`; }
    else if (row.type === "lane") { const m = row.lane.marks.find(m => m[0] <= time + 2 * (t1 - t0) / W() && time <= m[1] + 2 * (t1 - t0) / W());
      if (m) text = `${m[2]}: ${m[3]} (${(m[1] - m[0]).toFixed(1)} ms)`; }
    else { const t = row.thread, s = sampleAt(t, time); if (s >= 0) { const p = path(t.node[s]);
      const level = Math.floor((e.offsetY - row.y - 2) / LEVEL_H);
      const f = p[Math.min(level, p.length - 1)];
      if (f !== undefined) text = (t.wait[s] ? "[waiting] " : "") + `${funcs[f][0]}  (${funcs[f][1]}, depth ${Math.min(level, p.length - 1)} of ${p.length})`; } }
  }
  if (text) { tip.textContent = text; tip.style.display = "block"; tip.style.left = Math.min(e.clientX + 12, window.innerWidth - 530) + "px"; tip.style.top = e.clientY + 14 + "px"; }
  else tip.style.display = "none";
});
canvas.addEventListener("mouseleave", () => { tip.style.display = "none"; });
canvas.addEventListener("wheel", e => {
  e.preventDefault(); const at = tAt(e.offsetX), k = Math.exp(e.deltaY * 0.0015);
  const span = Math.min(Math.max((t1 - t0) * k, 2), D.durationMs * 1.2); const f = (at - t0) / (t1 - t0);
  t0 = at - f * span; t1 = t0 + span; draw();
}, { passive: false });
canvas.addEventListener("dblclick", () => { t0 = 0; t1 = Math.max(D.durationMs, 1); draw(); });
document.getElementById("idle").addEventListener("change", e => { showIdle = e.target.checked; draw(); });
window.addEventListener("resize", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
draw();
</script>
</body>
</html>
"""
