"""Trajectory recordings (Record.start JSONL): load, summarise, fetch from the test PC and plot.

The plot is a plain top-down SVG polyline in game units with north up: no map imagery and no
dependencies beyond the standard library.
"""

import argparse
import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path

PALETTE = ["#2c7bb6", "#00a6ca", "#00ccbc", "#90eb9d", "#f9d057", "#f29e2e", "#e76818", "#d7191c"]


def load(path):
    """Samples of a recording, oldest first. Lines that are not JSON objects are skipped."""
    samples = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sample = json.loads(line)
        except ValueError:
            continue
        if isinstance(sample, dict):
            samples.append(sample)
    return samples


def position(sample):
    pos = sample.get("pos")
    if isinstance(pos, list) and len(pos) == 3 and all(isinstance(v, (int, float)) for v in pos):
        return pos
    return None


def is_stuck(sample):
    nav = sample.get("nav") or {}
    return bool(sample.get("stuck")) or nav.get("state") == "recovering"


def summarize(samples):
    """Counts and distances for a recording: samples, duration, distance, bounds, stuck episodes."""
    points = [(s, position(s)) for s in samples]
    points = [(s, p) for s, p in points if p is not None]
    distance = 0.0
    teleports = 0
    for (_, a), (s, b) in zip(points, points[1:]):
        if s.get("teleport"):
            teleports += 1
            continue
        distance += math.dist(a, b)
    stuck_episodes = 0
    was_stuck = False
    for sample in samples:
        stuck = is_stuck(sample)
        if stuck and not was_stuck:
            stuck_episodes += 1
        was_stuck = stuck
    frame_ms = [s["frameMs"] for s in samples if isinstance(s.get("frameMs"), (int, float))]
    out = {
        "samples": len(samples),
        "durationS": round(samples[-1].get("t", 0) - samples[0].get("t", 0), 3) if samples else 0,
        "distance": round(distance, 1),
        "teleports": teleports,
        "stuckEpisodes": stuck_episodes,
    }
    if points:
        xs = [p[0] for _, p in points]
        ys = [p[1] for _, p in points]
        out["bounds"] = [min(xs), min(ys), max(xs), max(ys)]
        out["start"] = points[0][1]
        out["end"] = points[-1][1]
        out["displacement"] = round(math.dist(points[0][1][:2], points[-1][1][:2]), 1)
    if frame_ms:
        ordered = sorted(frame_ms)
        out["frameMs"] = {
            "mean": round(sum(frame_ms) / len(frame_ms), 2),
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            "max": ordered[-1],
        }
    return out


def load_episodes(path):
    """Stall, off-path and interruption episodes from a JSON file: a list, a Nav.status reply (its
    stalls.recent and interruptions.recent), or JSONL navStall/navInterrupted events."""
    text = Path(path).read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        nav = data.get("nav") or data.get("last") or data
        data = list((nav.get("stalls") or {}).get("recent") or [])
        data += [dict(e, kind="interruption") for e in (nav.get("interruptions") or {}).get("recent") or []]
    return [e for e in data if isinstance(e, dict)]


def episode_summary(episodes):
    """Count and total time per episode kind, plus the longest stall."""
    out = {}
    for kind in ("stall", "offPath", "interruption"):
        chosen = [e for e in episodes if e.get("kind", "interruption" if "reason" in e else None) == kind]
        durations = [float(e.get("durationS") or 0) for e in chosen]
        out[kind] = {"count": len(chosen), "totalS": round(sum(durations), 2),
                     "maxS": round(max(durations), 2) if durations else 0}
    stalls = [e for e in episodes if e.get("kind") == "stall"]
    causes = {}
    for e in stalls:
        cause = str(e.get("cause") or "unknown").split(":")[0]
        causes[cause] = causes.get(cause, 0) + 1
    out["stall"]["causes"] = causes
    out["stall"]["over2s"] = sum(1 for e in stalls if float(e.get("durationS") or 0) >= 2.0)
    return out


def _value(sample, color_by):
    if color_by == "frame":
        value = sample.get("frameMs")
    else:
        value = sample.get("speed")
    return float(value) if isinstance(value, (int, float)) else None


def _color(value, low, high):
    if value is None:
        return "#888888"
    span = high - low
    f = 0.0 if span <= 0 else min(max((value - low) / span, 0.0), 1.0)
    return PALETTE[min(int(f * len(PALETTE)), len(PALETTE) - 1)]


def _nice_step(length):
    """A round scale-bar length (1, 2 or 5 times a power of ten) near a fifth of `length`."""
    if length <= 0:
        return 1
    raw = length / 5
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if factor * power >= raw:
            return factor * power
    return 10 * power


EPISODE_STYLE = {"stall": "#7b3294", "offPath": "#e7298a", "interruption": "#0571b0"}


def plot_svg(samples, color_by="speed", width=900, title=None, episodes=()):
    """Top-down SVG of a recording: the path coloured by speed or frame time, a green start, a red
    end, orange crosses where navigation was stuck, and breaks where the player teleported.
    `episodes` (from load_episodes) adds rings sized by duration: purple stalls, pink off-path
    stretches and blue interruptions, each with its cause in a tooltip."""
    if color_by not in {"speed", "frame"}:
        raise ValueError("color_by is speed or frame")
    points = [(s, position(s)) for s in samples]
    points = [(s, p) for s, p in points if p is not None]
    if len(points) < 2:
        raise ValueError("the recording has fewer than two positions")
    xs = [p[0] for _, p in points]
    ys = [p[1] for _, p in points]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span = max(max_x - min_x, max_y - min_y, 1.0)
    margin = 40
    legend = 75 if episodes else 60
    scale = (width - 2 * margin) / span
    height = int((max_y - min_y) * scale + 2 * margin + legend)
    plot_w = (max_x - min_x) * scale + 2 * margin
    width = int(max(plot_w, 360))

    def xy(p):
        return (margin + (p[0] - min_x) * scale, margin + (max_y - p[1]) * scale)

    values = [v for v in (_value(s, color_by) for s, _ in points) if v is not None]
    if values:
        ordered = sorted(values)
        low = ordered[0]
        high = ordered[min(len(ordered) - 1, int(len(ordered) * 0.98))]
    else:
        low = high = 0.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    if title:
        parts.append(f'<text x="{margin}" y="22" font-size="14">{_escape(title)}</text>')
    for (_, a), (s, b) in zip(points, points[1:]):
        if s.get("teleport"):
            continue
        (x1, y1), (x2, y2) = xy(a), xy(b)
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{_color(_value(s, color_by), low, high)}" stroke-width="2.5" stroke-linecap="round"/>'
        )
    was_stuck = False
    for sample, p in points:
        stuck = is_stuck(sample)
        if stuck and not was_stuck:
            x, y = xy(p)
            parts.append(
                f'<path d="M{x - 6:.1f},{y - 6:.1f} L{x + 6:.1f},{y + 6:.1f} M{x - 6:.1f},{y + 6:.1f} L{x + 6:.1f},{y - 6:.1f}" '
                'stroke="#ff7f00" stroke-width="3"><title>stuck</title></path>'
            )
        was_stuck = stuck
    for episode in episodes:
        pos = episode.get("pos")
        if not (isinstance(pos, list) and len(pos) >= 2):
            continue
        kind = episode.get("kind", "interruption")
        x, y = xy(pos)
        duration = float(episode.get("durationS") or 0)
        radius = 4 + min(duration, 20.0) * 1.2
        note = episode.get("cause") or episode.get("reason") or ""
        if episode.get("action"):
            note = f"{note} ({episode['action']})"
        tip = _escape(f"{kind} {duration:.1f}s at {episode.get('atS', '?')}s {note}".strip())
        dash = ' stroke-dasharray="3,2"' if kind == "offPath" else ""
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="none" '
                     f'stroke="{EPISODE_STYLE.get(kind, "#444444")}" stroke-width="2"{dash}><title>{tip}</title></circle>')
    sx, sy = xy(points[0][1])
    ex, ey = xy(points[-1][1])
    parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="7" fill="#1a9641" stroke="#000"><title>start</title></circle>')
    parts.append(f'<rect x="{ex - 6:.1f}" y="{ey - 6:.1f}" width="12" height="12" fill="#d7191c" stroke="#000"><title>end</title></rect>')
    base = height - legend + 25
    step = _nice_step(span)
    bar = step * scale
    parts.append(f'<line x1="{margin}" y1="{base}" x2="{margin + bar:.1f}" y2="{base}" stroke="#000" stroke-width="2"/>')
    parts.append(f'<text x="{margin}" y="{base + 16}">{step:g} units</text>')
    label = "speed (units/s)" if color_by == "speed" else "frame time (ms)"
    lx = margin + bar + 40
    for i, color in enumerate(PALETTE):
        parts.append(f'<rect x="{lx + i * 18}" y="{base - 8}" width="18" height="10" fill="{color}"/>')
    parts.append(f'<text x="{lx}" y="{base + 16}">{low:.0f}</text>')
    parts.append(f'<text x="{lx + len(PALETTE) * 18}" y="{base + 16}" text-anchor="end">{high:.0f}</text>')
    parts.append(f'<text x="{lx + len(PALETTE) * 18 + 10}" y="{base}">{label}; north is up</text>')
    if episodes:
        parts.append(f'<text x="{margin}" y="{base + 32}">rings: <tspan fill="{EPISODE_STYLE["stall"]}">stall</tspan>, '
                     f'<tspan fill="{EPISODE_STYLE["offPath"]}">off path</tspan>, '
                     f'<tspan fill="{EPISODE_STYLE["interruption"]}">interruption</tspan>; size is duration</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scp_command(config, remote_path, local_path):
    """The scp invocation that copies a file from the test PC named by a host config."""
    destination = config["sshDestination"]
    if not isinstance(destination, str) or not destination or destination.startswith("-"):
        raise ValueError("Set an SSH destination or alias in the local configuration")
    remote = str(remote_path).replace("\\", "/")
    if len(remote) > 1 and remote[1] == ":":
        remote = "/" + remote  # Windows OpenSSH reads drive paths as /C:/...
    command = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    if path := config.get("sshConfig"):
        command += ["-F", str(Path(path).expanduser())]
    return command + [f"{destination}:{remote}", str(local_path)]


async def fetch(config, remote_path, local_path, timeout=120):
    """Copy a recording from the test PC to local_path (created directories included)."""
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *scp_command(config, remote_path, local_path), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        async with asyncio.timeout(timeout):
            _, error = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise RuntimeError(f"scp failed: {error.decode(errors='replace').strip()[:300]}")
    return local_path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-trajectory", description="Inspect Record.start recordings.")
    commands = parser.add_subparsers(dest="command", required=True)
    plot = commands.add_parser("plot", help="write a top-down SVG of a recording")
    plot.add_argument("file")
    plot.add_argument("-o", "--output", required=True)
    plot.add_argument("--color", choices=["speed", "frame"], default="speed")
    plot.add_argument("--width", type=int, default=900)
    plot.add_argument("--title")
    plot.add_argument("--episodes", help="Nav.status JSON or navStall/navInterrupted events to mark on the plot")
    summary = commands.add_parser("summary", help="print counts and distances as JSON")
    summary.add_argument("file")
    options = parser.parse_args(argv)
    samples = load(options.file)
    episodes = load_episodes(options.episodes) if getattr(options, "episodes", None) else []
    if options.command == "summary":
        print(json.dumps(summarize(samples), indent=2))
        return 0
    try:
        svg = plot_svg(samples, color_by=options.color, width=options.width,
                       title=options.title or Path(options.file).stem, episodes=episodes)
    except ValueError as error:
        print(f"skyrim-devtools-trajectory: {error}", file=sys.stderr)
        return 2
    Path(options.output).write_text(svg, encoding="utf-8")
    out = {"output": options.output, **summarize(samples)}
    if episodes:
        out["episodes"] = episode_summary(episodes)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
