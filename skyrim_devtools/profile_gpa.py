"""AMD GPUPerfAPI (GPA) hardware counters for a short capture window.

GPA is not shipped with skyrim-devtools: download a GPUPerfAPI 4.x release (MIT) on the game PC and point
`--gpa-dir`, the config key "gpaDir" or SKYRIM_DEVTOOLS_GPA_DIR at the folder holding GPUPerfAPIDX11-x64.dll.
The plugin loads it only for Profile.gpaCounters / Profile.gpaCapture. See docs/profiling.md, AMD counters.
"""

import os

ENV = "SKYRIM_DEVTOOLS_GPA_DIR"

# Counters whose value is a share of time or of requests; they average across samples weighted by GPU time.
_RATIO_USAGES = {"percentage"}
# Counters that add up across the samples of a frame.
_SUM_USAGES = {"milliseconds", "nanoseconds", "bytes", "kilobytes", "items", "cycles"}


def gpa_dir(explicit=None, config=None):
    """The GPA release folder from the argument, the environment or the config, or None."""
    return explicit or os.environ.get(ENV) or (config or {}).get("gpaDir") or None


async def list_counters(call, directory):
    """Counters GPA offers for the game's D3D11 device: {ok, device, counters[], defaultCounters, ...}."""
    return await call("Profile.gpaCounters", dir=directory, timeout=90)


async def capture(call, directory, counters=None, max_frames=600):
    """One GPA session: a baseline, then one frame per counter pass. Returns the plugin's result."""
    params = {"dir": directory, "maxFrames": max_frames}
    if counters:
        params["counters"] = list(counters)
    return await call("Profile.gpaCapture", timeout=180, **params)


def _number(value):
    return value if isinstance(value, (int, float)) else None


def summarize(result, top=8):
    """Per counter one frame value: sums for times/bytes/items, GPU-time-weighted means for percentages.

    Each pass runs on a different frame, so the values combine consecutive frames of the same scene,
    not one frame (docs/profiling.md, AMD counters)."""
    if not result or not result.get("ok"):
        return {"ok": False, "error": (result or {}).get("error"), "log": (result or {}).get("log")}
    counters = result.get("counters") or []
    labels = result.get("samples") or []
    by_name = {c.get("name"): c for c in counters}
    weights = [_number(v) or 0.0 for v in (by_name.get("GPUTime") or {}).get("values", [])]
    rows = []
    for counter in counters:
        values = [_number(v) for v in counter.get("values", [])]
        present = [v for v in values if v is not None]
        usage = counter.get("usage") or ""
        if not present:
            value = None
        elif usage in _SUM_USAGES:
            value = sum(present)
        elif usage in _RATIO_USAGES and weights and sum(weights) > 0:
            pairs = [(v, w) for v, w in zip(values, weights) if v is not None]
            total = sum(w for _, w in pairs)
            value = sum(v * w for v, w in pairs) / total if total else sum(present) / len(present)
        else:
            value = sum(present) / len(present)
        rows.append({"name": counter.get("name"), "group": counter.get("group"), "usage": usage,
                     "value": None if value is None else round(value, 4), "samples": len(present)})
    heavy = []
    if weights:
        order = sorted(range(len(weights)), key=lambda i: -weights[i])[:top]
        for i in order:
            sample = {"sample": i, "label": labels[i] if i < len(labels) else "", "gpuTimeMs": round(weights[i], 4)}
            for name in ("GPUBusy", "PSBusy", "VsGsBusy", "CSBusy", "MemUnitBusy", "L2CacheHit"):
                values = (by_name.get(name) or {}).get("values", [])
                if i < len(values) and _number(values[i]) is not None:
                    sample[name] = round(values[i], 2)
            heavy.append(sample)
    return {"ok": True, "device": result.get("device"), "passCount": result.get("passCount"),
            "framesUsed": result.get("framesUsed"), "sampleCount": result.get("sampleCount"),
            "counters": rows, "heaviestSamples": heavy, "missingCounters": result.get("missingCounters") or [],
            "warnings": result.get("warnings") or [],
            "mismatchedFrames": result.get("mismatchedFrames"), "paddedSamples": result.get("paddedSamples"),
            "mergedClears": result.get("mergedClears"), "overhead": result.get("overhead") or {}}


def render(summary):
    """Text lines for report.txt."""
    if not summary.get("ok"):
        return [f"== GPU counters (GPUPerfAPI): failed: {summary.get('error')}"]
    overhead = summary.get("overhead") or {}
    lines = [f"== GPU counters (GPUPerfAPI) on {summary.get('device')}: {summary.get('passCount')} passes over "
             f"{summary.get('framesUsed')} frames, {summary.get('sampleCount')} samples per frame; fps "
             f"{overhead.get('baselineFps')} -> {overhead.get('captureFps')} while capturing, "
             f"{overhead.get('cpuUsPerFrame')} us CPU per frame, init {overhead.get('initMs')} ms"]
    for row in summary["counters"]:
        value = "-" if row["value"] is None else f"{row['value']:,.3f}".rstrip("0").rstrip(".")
        lines.append(f"  {row['name']:<28} {value:>18}  {row['usage']}")
    if summary.get("missingCounters"):
        lines.append("  not offered on this GPU: " + ", ".join(summary["missingCounters"]))
    for sample in summary.get("heaviestSamples", [])[:5]:
        rest = "  ".join(f"{k} {v}" for k, v in sample.items() if k not in ("sample", "label", "gpuTimeMs"))
        lines.append(f"  sample {sample['sample']:>3} {sample['gpuTimeMs']:8.3f} ms  {sample['label']}  {rest}")
    return lines
