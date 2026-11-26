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


def frame_table(profile):
    """One row per presented frame: start/end (ms), frame ms, sampled main-thread CPU ms, GPU ms and which
    side bounded it (gpu, cpu, or capped when both finished well inside the frame)."""
    raw = profile.raw
    by_frame = {g["frame"]: g for g in gpu_frames(raw)[0]}
    busy, period = _busy_series(profile)
    hitch_ms = raw.get("hitchThresholdMs", 100.0)
    rows = []
    for start, end, frame in raw.get("frameMarks", []):
        cpu_ms = (bisect.bisect_left(busy, end) - bisect.bisect_left(busy, start)) * period
        gpu = by_frame.get(frame)
        frame_ms = end - start
        gpu_ms = gpu["gpuMs"] if gpu else None
        bound = None
        if gpu_ms is not None and frame_ms > 0:
            if max(cpu_ms, gpu_ms) < 0.85 * frame_ms:
                bound = "capped"
            else:
                bound = "gpu" if gpu_ms > cpu_ms else "cpu"
        rows.append({"frame": frame, "start": start, "end": end, "frameMs": round(frame_ms, 3), "cpuMs": round(cpu_ms, 3),
                     "gpuMs": gpu_ms, "gpuStart": gpu["gpuStart"] if gpu else None, "gpuEnd": gpu["gpuEnd"] if gpu else None,
                     "hitch": frame_ms > hitch_ms, "bound": bound})
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
