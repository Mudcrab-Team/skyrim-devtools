"""GPU timing, per-frame CPU/GPU split and counter series of a profile.

The plugin brackets each frame's GPU work with D3D11 timestamp queries and stamps every render-target and
depth clear. GPU timestamps run on their own clock, so they are placed on the CPU clock by the tightest
bound the data gives: a query cannot execute before the CPU issued it, so the offset is the largest
(CPU issue time - GPU time) over all timed frames. The GPU then appears as early as the data allows; when
the GPU never caught up with the CPU in the whole profile the true times are later than drawn.
"""

import bisect
from statistics import median


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1)))], 3)


def pass_kind(label, backbuffer):
    """A guess at what a pass draws from the target its opening clear touched; see docs/profiling.md."""
    if label in ("frame", "present"):
        return label
    parts = label.split()
    kind = parts[0] if parts else ""
    try:
        width, height = (int(v) for v in parts[1].split("x"))
    except (IndexError, ValueError):
        return "other"
    back_w, back_h = (backbuffer or [0, 0])[:2]
    if kind == "dsv" and (width == height or "array" in parts) and (width, height) != (back_w, back_h):
        return "shadows"
    if (width, height) == (back_w, back_h):
        return "main" if kind == "dsv" else "scene"
    if back_w and width <= back_w // 2 and height <= back_h // 2:
        return "post"
    return "offscreen"


def gpu_frames(raw):
    """Timed frames on the CPU clock: {frame, cpuBegin, cpuEnd, gpuStart, gpuEnd, gpuMs, hookUs, passes}
    with passes [(label, kind, start, end)], plus the alignment offset in ms (None without data)."""
    gpu = raw.get("gpu") or {}
    labels = gpu.get("labels") or []
    backbuffer = gpu.get("backbuffer")
    frames = [f for f in gpu.get("frames") or [] if len(f) >= 6 and len(f[5]) >= 2]
    if not frames:
        return [], None
    offset = max(max(f[1] - f[5][0][1] * 1000.0, f[2] - f[5][-1][1] * 1000.0) for f in frames)
    out = []
    for frame, cpu_begin, cpu_end, hook_us, dropped, stamps in frames:
        times = [(labels[l] if l < len(labels) else str(l), t * 1000.0 + offset) for l, t in stamps]
        passes = [(times[i][0], pass_kind(times[i][0], backbuffer), round(times[i][1], 3), round(times[i + 1][1], 3))
                  for i in range(len(times) - 1)]
        out.append({"frame": frame, "cpuBegin": cpu_begin, "cpuEnd": cpu_end, "gpuStart": round(times[0][1], 3),
                    "gpuEnd": round(times[-1][1], 3), "gpuMs": round(times[-1][1] - times[0][1], 3), "hookUs": hook_us,
                    "droppedPasses": dropped, "passes": passes})
    return out, round(offset, 3)


def _busy_series(profile):
    """Sorted sample times (ms) of the main thread's on-CPU samples."""
    period = 1000.0 / profile.raw.get("hz", 1000.0)
    times = []
    for thread, at, stack, count, waiting, _ in profile.samples("main"):
        if not waiting:
            times.extend(at + i * period for i in range(count))
    times.sort()
    return times, period


BOUND_SHARE = 0.85      # a side that is busy for this share of the frame sets its pace
PACE_TOLERANCE_MS = 1.0  # GPU span within this of the CPU's issue span: the GPU kept up with the CPU
UTIL_IDLE_PCT = 60.0     # board utilisation below this rules out a GPU-bound frame
UTIL_BUSY_PCT = 85.0     # and above this marks one even when the GPU followed the CPU (sync stalls)
BOUND_LABELS = {"gpu": "gpu", "cpu": "cpu", "capped": "capped (vsync or frame limiter)"}


def classify(frame_ms, cpu_ms, gpu_ms, excess_ms=None, util_pct=None):
    """Which side set the frame's pace: gpu, cpu or capped.

    The GPU span alone cannot tell: timestamps advance while the GPU waits for the CPU's next commands, so
    a game that sleeps in its frame limiter between draws shows a GPU span as long as the frame. `excess_ms`
    (GPU span minus the CPU's span from the frame-start stamp to Present) says whether the GPU fell behind;
    near zero it only followed the CPU. Board utilisation, when known, overrides both ways."""
    if not frame_ms or frame_ms <= 0 or gpu_ms is None:
        return None
    tolerance = max(PACE_TOLERANCE_MS, 0.1 * frame_ms)
    paced = excess_ms is not None and excess_ms < tolerance
    gpu_busy = not paced and gpu_ms >= BOUND_SHARE * frame_ms
    if util_pct is not None:
        if util_pct < UTIL_IDLE_PCT:
            gpu_busy = False
        elif util_pct >= UTIL_BUSY_PCT:
            gpu_busy = True
    cpu_busy = cpu_ms is not None and cpu_ms >= BOUND_SHARE * frame_ms
    if gpu_busy and cpu_busy:
        return "gpu" if gpu_ms > cpu_ms else "cpu"
    if gpu_busy:
        return "gpu"
    return "cpu" if cpu_busy else "capped"


def util_series(raw):
    """Sorted [(ms, GPU utilisation %)] for the classifier's cross-check: the WDDM engine counter here; a
    vendor board reading, where a build records one, is preferred by extending this function."""
    return sorted((row[0], row[1]) for row in raw.get("gpuCounters") or [] if len(row) > 1 and row[1] is not None)


def _util_at(series, times, at, within=2000.0):
    """The utilisation sample covering `at`: counters are rates over the preceding interval, so the first
    sample at or after it."""
    i = bisect.bisect_left(times, at)
    if i < len(series) and series[i][0] - at <= within:
        return series[i][1]
    return None


def frame_table(profile):
    """One row per presented frame: start/end (ms), frame ms, sampled main-thread CPU ms, GPU span ms, how much
    longer the GPU took than the CPU issued for (excessMs), the utilisation reading, a GPU busy estimate and
    which side set the pace (see classify)."""
    raw = profile.raw
    by_frame = {g["frame"]: g for g in gpu_frames(raw)[0]}
    busy, period = _busy_series(profile)
    util = util_series(raw)
    util_times = [u[0] for u in util]
    hitch_ms = raw.get("hitchThresholdMs", 100.0)
    rows = []
    for start, end, frame in raw.get("frameMarks", []):
        cpu_ms = (bisect.bisect_left(busy, end) - bisect.bisect_left(busy, start)) * period
        gpu = by_frame.get(frame)
        frame_ms = end - start
        gpu_ms = gpu["gpuMs"] if gpu else None
        excess = round(gpu_ms - (gpu["cpuEnd"] - gpu["cpuBegin"]), 3) if gpu else None
        util_pct = _util_at(util, util_times, end) if gpu else None
        paced = excess is not None and excess < max(PACE_TOLERANCE_MS, 0.1 * frame_ms)
        if gpu_ms is None:
            busy_ms = None
        elif not paced:
            busy_ms = gpu_ms
        else:
            busy_ms = round(min(gpu_ms, util_pct / 100.0 * frame_ms), 3) if util_pct is not None else None
        rows.append({"frame": frame, "start": start, "end": end, "frameMs": round(frame_ms, 3), "cpuMs": round(cpu_ms, 3),
                     "gpuMs": gpu_ms, "gpuStart": gpu["gpuStart"] if gpu else None, "gpuEnd": gpu["gpuEnd"] if gpu else None,
                     "excessMs": excess, "paced": paced if gpu else None, "utilPct": util_pct, "gpuBusyMs": busy_ms,
                     "hitch": frame_ms > hitch_ms, "bound": classify(frame_ms, cpu_ms, gpu_ms, excess, util_pct)})
    return rows


def gpu_summary(profile, top=10):
    raw = profile.raw
    gpu = raw.get("gpu") or {}
    frames, offset = gpu_frames(raw)
    out = {"available": bool(gpu.get("available")), "reason": gpu.get("reason") or None, "frames": len(frames),
           "presented": len(raw.get("frameMarks", [])), "alignmentOffsetMs": offset,
           "disjointFrames": gpu.get("disjointFrames"), "staleFrames": gpu.get("staleFrames")}
    if not frames:
        return out
    table = [r for r in frame_table(profile) if r["gpuMs"] is not None]
    bounds = [r["bound"] for r in table if r["bound"]]
    passes = {}
    for f in frames:
        for label, kind, start, end in f["passes"]:
            entry = passes.setdefault(label, [kind, 0.0])
            entry[1] += end - start
    total_gpu = sum(f["gpuMs"] for f in frames) or 1.0
    out.update({
        "timedPct": round(100.0 * len(frames) / max(1, out["presented"]), 1),
        "gpuMsP50": _pct([f["gpuMs"] for f in frames], 0.5), "gpuMsP95": _pct([f["gpuMs"] for f in frames], 0.95),
        "gpuMsMax": round(max(f["gpuMs"] for f in frames), 3),
        "cpuMsP50": _pct([r["cpuMs"] for r in table], 0.5), "frameMsP50": _pct([r["frameMs"] for r in table], 0.5),
        "boundPct": {k: round(100.0 * bounds.count(k) / len(bounds), 1) for k in ("gpu", "cpu", "capped")} if bounds else {},
        "pacedPct": round(100.0 * sum(1 for r in table if r["paced"]) / len(table), 1) if table else None,
        "excessMsP50": _pct([r["excessMs"] for r in table], 0.5),
        "gpuBusyMsP50": _pct([r["gpuBusyMs"] for r in table if r["gpuBusyMs"] is not None], 0.5),
        "utilPctMean": round(sum(us) / len(us), 1) if (us := [r["utilPct"] for r in table if r["utilPct"] is not None]) else None,
        "hookUsPerFrame": round(median(f["hookUs"] for f in frames), 1),
        "passes": [{"pass": label, "kind": kind, "msPerFrame": round(ms / len(frames), 3), "pct": round(100.0 * ms / total_gpu, 1)}
                   for label, (kind, ms) in sorted(passes.items(), key=lambda p: -p[1][1])[:top]],
    })
    return out


def counter_series(raw):
    """Counters as [(ms, name, value)]: rates per second between 250 ms samples, levels as they were."""
    out = []
    rows = raw.get("counters") or []
    for prev, cur in zip(rows, rows[1:]):
        dt = (cur[0] - prev[0]) / 1000.0
        if dt <= 0:
            continue
        at = cur[0]
        out.append((at, "page_faults_per_s", round((cur[1] - prev[1]) / dt, 1)))
        out.append((at, "read_mb_per_s", round((cur[3] - prev[3]) / dt / 1048576.0, 3)))
        out.append((at, "write_mb_per_s", round((cur[4] - prev[4]) / dt / 1048576.0, 3)))
        out.append((at, "process_cpu_pct_of_one_core", round((cur[5] - prev[5]) / (dt * 1e5), 1)))
    for row in rows:
        out.append((row[0], "working_set_mb", round(row[2] / 1048576.0, 1)))
        out.append((row[0], "available_physical_mb", round(row[6] / 1048576.0, 1)))
    for at, util, vram in raw.get("gpuCounters") or []:
        if util is not None:
            out.append((at, "gpu_util_pct", util))
        if vram is not None:
            out.append((at, "gpu_vram_mb", vram))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


GPU_FRAME_SCHEMA = {
    "name": "GpuFrame", "display": ["marker-chart", "marker-table", "timeline-overview"],
    "chartLabel": "GPU {marker.data.frame}: {marker.data.bound}",
    "tooltipLabel": "GPU frame {marker.data.frame} ({marker.data.bound})",
    "tableLabel": "GPU frame {marker.data.frame}: {marker.data.gpuMs} ms, {marker.data.bound}",
    "data": [{"key": "frame", "label": "Frame", "format": "integer", "searchable": True},
             {"key": "gpuMs", "label": "GPU span", "format": "milliseconds"},
             {"key": "gpuBusyMs", "label": "GPU busy (estimate)", "format": "milliseconds"},
             {"key": "cpuMs", "label": "Main thread on-CPU", "format": "milliseconds"},
             {"key": "excessMs", "label": "GPU span - CPU issue span", "format": "milliseconds"},
             {"key": "utilPct", "label": "GPU utilisation %", "format": "decimal"},
             {"key": "bound", "label": "Bound by", "format": "string", "searchable": True}]}
GPU_PASS_SCHEMA = {
    "name": "GpuPass", "display": ["marker-chart", "marker-table"],
    "chartLabel": "{marker.data.pass}", "tooltipLabel": "{marker.data.pass} ({marker.data.kind})",
    "tableLabel": "{marker.data.pass} ({marker.data.kind}) {marker.data.ms} ms",
    "data": [{"key": "pass", "label": "Pass", "format": "string", "searchable": True},
             {"key": "kind", "label": "Kind", "format": "string", "searchable": True},
             {"key": "frame", "label": "Frame", "format": "integer"},
             {"key": "ms", "label": "Span", "format": "milliseconds"}]}


def firefox_tracks(export, profile):
    """GPU frames and passes as markers on the main thread (positions are the aligned lower bound, see
    gpu_frames), GPU frame time and utilisation as counter tracks."""
    frames, _ = gpu_frames(profile.raw)
    if not frames:
        return
    rows = {r["frame"]: r for r in frame_table(profile)}
    export.marker_schema(GPU_FRAME_SCHEMA)
    export.marker_schema(GPU_PASS_SCHEMA)
    markers = []
    for f in frames:
        row = rows.get(f["frame"], {})
        markers.append((f"GPU frame {f['frame']}", f["gpuStart"], f["gpuEnd"],
                        {"type": "GpuFrame", "frame": f["frame"], "gpuMs": f["gpuMs"], "gpuBusyMs": row.get("gpuBusyMs"),
                         "cpuMs": row.get("cpuMs"), "excessMs": row.get("excessMs"), "utilPct": row.get("utilPct"),
                         "bound": BOUND_LABELS.get(row.get("bound"), "untimed")}))
        merged = []
        for label, kind, start, end in f["passes"]:
            if label == "frame" and end - start < 0.01:
                continue
            # A pass reopened by another clear of the same target stays one span in the chart.
            if merged and merged[-1][0] == label and abs(merged[-1][3] - start) < 1e-6:
                merged[-1][3] = end
            else:
                merged.append([label, kind, start, end])
        for label, kind, start, end in merged:
            markers.append((label, start, end, {"type": "GpuPass", "pass": label, "kind": kind, "frame": f["frame"],
                                                "ms": round(end - start, 3)}))
    export.add_markers(markers)
    export.add_counter("gpu_frame_ms", "GPU frame ms", [f["gpuEnd"] for f in frames], [f["gpuMs"] for f in frames],
                       unit="ms", color="orange", description="GPU span of each timed frame (frame start to Present)")
    util = util_series(profile.raw)
    if util:
        export.add_counter("gpu_util_pct", "GPU util", [u[0] for u in util], [u[1] for u in util], unit="percent",
                           color="red", description="GPU engine utilisation")
