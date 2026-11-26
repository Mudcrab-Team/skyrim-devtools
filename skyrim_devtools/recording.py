"""Recordings written by Recording.stop (skyrim-devtools-recording, version 1): load, validate, compare.

A recording is one JSON object: `meta`, `entry` (how the scene was entered), `events` (cell changes, console
commands) and `samples`, one row per line:
`[tMs, x, y, z, yawDeg, pitchDeg, pov, camX, camY, camZ, camYawDeg, camPitchDeg]`.
Everything here is plain Python so a recording can be checked, edited and compared without the game.
"""

import json
import math
from pathlib import Path

FORMAT = "skyrim-devtools-recording"
VERSION = 1
SAMPLE_COLUMNS = ["tMs", "x", "y", "z", "yawDeg", "pitchDeg", "pov", "camX", "camY", "camZ", "camYawDeg", "camPitchDeg"]
TIERS = ("anchored", "cell", "worldspace")
DEFAULT_ANCHOR_MS = 10_000
DEFAULT_CELL_MS = 60_000
# Two consecutive poses further apart than this are a teleport (a door, coc), not movement.
JUMP_UNITS = 1500.0


class RecordingError(ValueError):
    pass


def load(path):
    """Parse and validate a recording file."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecordingError(f"cannot read {path}: {error}") from error
    validate(doc)
    return doc


def validate(doc):
    """Raise RecordingError unless `doc` has the shape Recording.replay reads."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise RecordingError(f"not a {FORMAT} file")
    if doc.get("version") != VERSION:
        raise RecordingError(f"unsupported version {doc.get('version')!r}")
    rows = doc.get("samples")
    if not isinstance(rows, list) or len(rows) < 2:
        raise RecordingError("fewer than two samples")
    last = -1.0
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 6 or not all(isinstance(v, (int, float)) for v in row[:6]):
            raise RecordingError(f"sample {index} needs six numbers (t, x, y, z, yaw, pitch)")
        if row[0] < last:
            raise RecordingError(f"sample {index} goes back in time")
        last = row[0]
    for index, event in enumerate(doc.get("events", [])):
        if not isinstance(event, dict) or not isinstance(event.get("t"), (int, float)) or "type" not in event:
            raise RecordingError(f"event {index} needs t and type")
    tier = (doc.get("meta", {}).get("coupling") or {}).get("tier")
    if tier is not None and tier not in TIERS:
        raise RecordingError(f"coupling tier {tier!r} is not one of {TIERS}")
    return doc


def coupling_tier(doc, anchor_ms=DEFAULT_ANCHOR_MS, cell_ms=DEFAULT_CELL_MS):
    """The scene coupling a replay uses: from the entry's age before recording, unless the recording pins one."""
    over = (doc.get("meta") or {}).get("coupling") or {}
    if over.get("tier"):
        return over["tier"]
    entry = doc.get("entry") or {}
    age = entry.get("ageMs")
    if entry.get("kind", "unknown") == "unknown" or not isinstance(age, (int, float)):
        return "worldspace"
    anchor = over.get("anchorMs", anchor_ms)
    cell = over.get("cellMs", cell_ms)
    return "anchored" if age <= anchor else "cell" if age <= cell else "worldspace"


def duration_ms(doc):
    return doc["samples"][-1][0] - doc["samples"][0][0]


def summary(doc):
    """What a person wants to know before replaying: length, distance, entry, coupling, cell changes, commands."""
    rows = doc["samples"]
    distance = 0.0
    for a, b in zip(rows, rows[1:]):
        step = math.dist(a[1:4], b[1:4])
        if step < JUMP_UNITS:
            distance += step
    events = doc.get("events", [])
    return {
        "name": (doc.get("meta") or {}).get("name"),
        "durationS": round(duration_ms(doc) / 1000.0, 1),
        "samples": len(rows),
        "distance": round(distance),
        "entry": {k: v for k, v in (doc.get("entry") or {}).items() if k in ("kind", "save", "coc", "ageMs")},
        "tier": coupling_tier(doc),
        "cellChanges": sum(1 for e in events if e.get("type") == "cell"),
        "commands": sum(1 for e in events if e.get("type") == "command"),
    }


def pose_at(rows, t_ms):
    """The recorded (x, y, z) at time t, interpolated; None across a teleport so a door is not averaged away."""
    if not rows:
        return None
    if t_ms <= rows[0][0]:
        return tuple(rows[0][1:4])
    if t_ms >= rows[-1][0]:
        return tuple(rows[-1][1:4])
    low, high = 0, len(rows) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if rows[mid][0] <= t_ms:
            low = mid
        else:
            high = mid
    a, b = rows[low], rows[high]
    if math.dist(a[1:4], b[1:4]) >= JUMP_UNITS:
        return None
    span = b[0] - a[0]
    f = (t_ms - a[0]) / span if span > 0 else 0.0
    return tuple(a[i] + (b[i] - a[i]) * f for i in (1, 2, 3))


def percentile(values, p):
    """Linear-interpolated percentile (p in 0..100) of a list of numbers; None for an empty list."""
    values = sorted(values)
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    rank = (len(values) - 1) * p / 100.0
    low = int(math.floor(rank))
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (rank - low)


def divergence(trace_rows, reference_rows):
    """Distance between a replay's observed poses and a reference path at the same times.

    `trace_rows` are `[tMs, x, y, z, ...]` rows (Recording.replayTrace), `reference_rows` either a recording's sample
    rows or another trace; times are matched by interpolation and pairs across a teleport are skipped.
    Returns {count, medianUnits, p95Units, maxUnits, meanUnits}."""
    distances = []
    for row in trace_rows:
        ref = pose_at(reference_rows, row[0])
        if ref is not None:
            distances.append(math.dist(row[1:4], ref))
    if not distances:
        return {"count": 0, "medianUnits": None, "p95Units": None, "maxUnits": None, "meanUnits": None}
    return {"count": len(distances), "medianUnits": round(percentile(distances, 50), 2),
            "p95Units": round(percentile(distances, 95), 2), "maxUnits": round(max(distances), 2),
            "meanUnits": round(sum(distances) / len(distances), 2)}
