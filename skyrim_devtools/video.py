"""Compose a showcase video: game footage beside an "agent view" panel, boring stretches sped up.

  skyrim-devtools-video compose --footage x.mp4 --timeline t.jsonl -o out.mp4 [--speedup-idle 4] [--title ...]
  ... compose ... --highlights [--skip 71:00-73:40] [--target-length 270]   # a highlight cut around narration
  ... compose ... --keep 0:30-6:00 --keep 14:00-15:30                     # only these stretches of footage
  ... compose ... --emit-dir work/ --duration 6640                         # write the inputs; run ffmpeg elsewhere

Besides the recorder's own records, a timeline may carry presentation records written by a script:
  {"t":"chapter","ms":..,"title":"..","subtitle":"..","durationS":3}      full-screen title card inserted here
  {"t":"slide","ms":..,"image":"x.png","title":"..","caption":"..","durationS":6}  full-screen still inserted here
  {"t":"inset","ms":..,"image":"x.png","label":"..","durationS":8}        picture-in-picture over the game
  {"t":"metrics","ms":..,"title":"..","rows":[["FPS","60"],..],"spark":[..],"badge":".."}  live box in the panel
                                                                          ("hide": true removes it)
Image paths are relative to the timeline's folder. Movement trouble (moveStuck events) and raised alerts
show as a red banner over the caption bar.

The panel (recent tool calls, status, captions) is drawn with Pillow as one still image per change in
source time, turned into a video, and overlaid with the footage in a single ffmpeg run that also applies
the speed-up segments and the title cards. Needs `pip install -e .[video]` and ffmpeg 7 or newer on PATH.
"""

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

W, H = 1920, 1080
GAME_W, GAME_H = 1344, 756
GAME_X, GAME_Y = 0, 36
PANEL_X = GAME_W
FPS = 30
BG = (14, 17, 23)
PANEL_BG = (21, 26, 35)
CARD_BG = (26, 33, 46)
FG = (230, 235, 242)
DIM = (130, 142, 160)
ACCENT = (96, 165, 250)
GOOD = (74, 222, 128)
BAD = (248, 113, 113)
WARN = (250, 204, 21)

# Tools whose duration is waiting or walking rather than something to watch.
BORING_TOOLS = ("wait_for", "game_start", "load_save", "reload", "center_on_cell", "nav_", "walk", "travel", "capture_",
                "fast_travel", "wait", "scenario_setup")
# Instant state reads: nothing to watch, so they only hold the video at normal speed for a moment.
QUERY_TOOLS = ("combat_state", "player_state", "player_inventory", "player_equipped", "player_spells", "world_nearby",
               "world_crosshair", "list_menus", "quest_get", "quest_active", "quest_find", "nav_status", "nav_nearby",
               "nav_places", "snapshot", "game_status", "camera_state", "events_", "list_saves", "message_box", "look",
               "turn_to", "close_menu")
# Conversations are what a viewer wants to read at normal speed.
DIALOGUE_TOOLS = ("dialogue_", "message_box")
FONT_CANDIDATES = {
    "sans": ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"],
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/HelveticaNeue.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "mono": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "DejaVuSansMono.ttf"],
}


def need_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise SystemExit("skyrim-devtools-video: install Pillow (pip install -e '.[video]')")
    return Image, ImageDraw, ImageFont


_fonts = {}


def font(kind, size):
    key = (kind, size)
    if key not in _fonts:
        _, _, ImageFont = need_pillow()
        for candidate in FONT_CANDIDATES[kind]:
            try:
                _fonts[key] = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default(size)
    return _fonts[key]


# ---- timeline ----------------------------------------------------------------------------------


@dataclass
class Call:
    name: str
    args: dict
    start: float
    end: float
    ok: bool
    summary: str
    error: str


@dataclass
class Timeline:
    calls: list = field(default_factory=list)
    captions: list = field(default_factory=list)  # (start, end, text)
    statuses: list = field(default_factory=list)  # (t, dict)
    alerts: list = field(default_factory=list)  # (start, end or None, kind, severity)
    banners: list = field(default_factory=list)  # (start, end, text)
    insets: list = field(default_factory=list)  # (start, end, image path, label)
    slides: list = field(default_factory=list)  # (t, duration, spec dict)
    metrics: list = field(default_factory=list)  # (t, metrics dict or None)
    footage_start_ms: int | None = None
    footage_seconds: float | None = None  # from the capture stop record, when the timeline has one


def load_timeline(path, footage_start_ms=None):
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if footage_start_ms is None:
        starts = [r["startEpochMs"] for r in records if r.get("t") == "capture" and r.get("action") == "start"]
        if not starts:
            raise SystemExit("no capture start in the timeline: pass --footage-start-ms")
        footage_start_ms = starts[-1]
    t0 = footage_start_ms
    timeline = Timeline(footage_start_ms=t0)
    stops = [r["stopEpochMs"] for r in records if r.get("t") == "capture" and r.get("action") == "stop"
             and r.get("startEpochMs") == t0 and r.get("stopEpochMs")]
    if stops:
        timeline.footage_seconds = (stops[-1] - t0) / 1000
    narrations = []
    open_alerts = {}
    stuck = None
    base = Path(path).parent

    def resolve(name):
        image = Path(name)
        return image if image.is_absolute() else base / image

    for record in records:
        kind = record.get("t")
        when = (record["ms"] - t0) / 1000 if "ms" in record else None
        if kind == "event" and record.get("type") == "moveStuck":
            data = record.get("data") or {}
            reason = data.get("reason") or "no progress"
            stuck = len(timeline.banners)
            # an unstuck event closes the banner early; otherwise it stays long enough to read
            timeline.banners.append([when, when + 6.0, f"STUCK DETECTED: {reason}"])
            continue
        if kind == "event" and record.get("type") == "moveUnstuck":
            if stuck is not None:
                timeline.banners[stuck][1] = max(timeline.banners[stuck][0] + 2.5, min(timeline.banners[stuck][1], when))
                stuck = None
            continue
        if kind == "chapter" or kind == "slide":
            spec = {k: v for k, v in record.items() if k not in ("t", "ms")}
            spec["kind"] = kind
            if kind == "slide":
                spec["image"] = str(resolve(record["image"]))
            timeline.slides.append((when, float(record.get("durationS", 3.0 if kind == "chapter" else 6.0)), spec))
            continue
        if kind == "inset":
            timeline.insets.append((when, when + float(record.get("durationS", 8.0)), str(resolve(record["image"])),
                                    record.get("label", "")))
            continue
        if kind == "metrics":
            panel = None if record.get("hide") else {k: v for k, v in record.items() if k not in ("t", "ms")}
            timeline.metrics.append((when, panel))
            continue
        if kind == "alert":
            when = (record["ms"] - t0) / 1000
            if record.get("action") == "raised":
                open_alerts[record["kind"]] = len(timeline.alerts)
                timeline.alerts.append([when, None, record["kind"], record.get("severity", "warn")])
                timeline.banners.append([when, when + 6.0, f"ALERT: {record['kind']}" + (
                    f": {record['hint']}" if record.get("hint") else "")])
            elif record["kind"] in open_alerts:
                timeline.alerts[open_alerts.pop(record["kind"])][1] = when
            continue
        if kind == "tool":
            timeline.calls.append(Call(record["name"], record.get("args", {}), (record["startMs"] - t0) / 1000,
                                       (record["endMs"] - t0) / 1000, record.get("ok", True),
                                       record.get("summary", ""), record.get("error", "")))
        elif kind == "narrate":
            narrations.append(((record["ms"] - t0) / 1000, record["text"], record.get("holdS")))
        elif kind == "status":
            timeline.statuses.append(((record["ms"] - t0) / 1000, {k: v for k, v in record.items() if k not in ("t", "ms")}))
    narrations.sort()
    timeline.metrics.sort(key=lambda item: item[0])
    timeline.slides.sort(key=lambda item: item[0])
    for index, (start, text, hold_s) in enumerate(narrations):
        # holdS keeps a caption up for a long stretch of footage, for example a frozen game shown sped up
        hold = float(hold_s) if hold_s else max(4.0, 0.07 * len(text) + 1.5)
        end = start + hold
        if index + 1 < len(narrations):
            end = min(end, narrations[index + 1][0])
        timeline.captions.append((start, end, text))
    timeline.calls.sort(key=lambda c: c.start)
    return timeline


def is_boring(name):
    return name.startswith(BORING_TOOLS)


def is_dialogue(name):
    return name.startswith(DIALOGUE_TOOLS)


# ---- speed plan --------------------------------------------------------------------------------


def merge(windows):
    out = []
    for start, end in sorted(windows):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


LONG_CAPTION_S = 60.0


def plan_segments(timeline, duration, speedup=4.0, idle_threshold=3.0, max_idle_out=6.0, speedup_walk=None,
                  speedup_action=1.0):
    """Return [(start, end, speed)] over source time covering [0, duration]; gaps with nothing to watch run faster."""
    windows = []
    for call in timeline.calls:
        if call.end < 0 or call.start > duration:
            continue
        if is_dialogue(call.name):
            windows.append((call.start - 1.0, call.end + 2.0))
        elif call.name.startswith(QUERY_TOOLS) and call.end - call.start < 2.0:
            windows.append((call.start - 0.15, call.end + 0.4))
        elif is_boring(call.name) or (not call.ok and call.end - call.start > 3.0):
            # a call that failed after a long wait (a stalled game) is a wait too
            # the moment of the call and of its result stay at normal speed; the middle is the wait
            windows.append((call.start - 0.5, call.start + 1.0))
            windows.append((call.end - 0.5, call.end + 1.0))
        else:
            windows.append((call.start - 0.5, call.end + 1.5))
    for start, end, _ in timeline.captions:
        # a caption held for minutes (holdS) labels a stretch that should still run fast
        if end - start <= LONG_CAPTION_S:
            windows.append((start - 0.3, end))
    # a picture-in-picture and the moment a banner goes up are there to be read, so they play at normal speed
    windows += [(start, end) for start, end, _, _ in timeline.insets]
    windows += [(start - 1.5, start + 4.0) for start, _, _ in timeline.banners]
    windows = merge([(max(0.0, s), min(duration, e)) for s, e in windows if e > 0 and s < duration])
    segments = []
    cursor = 0.0
    for start, end in windows:
        if start - cursor > 0:
            segments.append(gap_segment(cursor, start, speedup, idle_threshold, max_idle_out))
        segments.append((start, end, 1.0))
        cursor = end
    if duration - cursor > 0:
        segments.append(gap_segment(cursor, duration, speedup, idle_threshold, max_idle_out))
    segments = [s for s in segments if s[1] - s[0] > 0.02]
    if speedup_action > 1.0:
        # tool activity with no caption or conversation to read (fights, searching) runs a little faster
        watch = [(a - 0.3, b) for a, b, _ in timeline.captions if b - a <= LONG_CAPTION_S]
        watch += [(c.start - 1.0, c.end + 2.0) for c in timeline.calls if is_dialogue(c.name)]
        segments = [(a, b, speedup_action) if speed == 1.0 and not any(w0 < b and a < w1 for w0, w1 in watch)
                    else (a, b, speed) for a, b, speed in segments]
    if speedup_walk and speedup_walk > speedup:
        # long walks and waits run faster than other idle stretches
        walks = [(c.start, c.end) for c in timeline.calls if is_boring(c.name) and c.end - c.start > idle_threshold]
        segments = [(a, b, max(speed, speedup_walk, (b - a) / max_idle_out)) if speed > 1.0 and any(
            w0 <= (a + b) / 2 <= w1 for w0, w1 in walks) else (a, b, speed) for a, b, speed in segments]
    return segments


def parse_time(text):
    """Seconds from "SS", "M:SS" or "H:MM:SS" (fractions allowed)."""
    seconds = 0.0
    for part in str(text).strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_interval(text):
    start, sep, end = str(text).partition("-")
    if not sep:
        raise ValueError(f"interval {text!r} is START-END")
    start, end = parse_time(start), parse_time(end)
    if end <= start:
        raise ValueError(f"interval {text!r} ends before it starts")
    return (start, end)


def narration_windows(timeline, before=4.0, after=6.0):
    """Footage stretches worth keeping in a highlight cut: from `before` seconds ahead of each narration
    marker to `after` seconds past the end of its caption. Narration is written at the moments the agent
    found worth explaining, so it is the cheapest available signal of what a viewer wants to see."""
    return merge([(max(0.0, start - before), end + after) for start, end, _ in timeline.captions])


def subtract(windows, holes):
    out = []
    for start, end in windows:
        pieces = [(start, end)]
        for h0, h1 in holes:
            pieces = [p for a, b in pieces for p in ((a, min(b, h0)), (max(a, h1), b)) if p[1] - p[0] > 1e-6]
        out += pieces
    return [list(p) for p in sorted(out)]


def restrict(segments, keep):
    """The parts of the speed plan inside the kept windows; everything else is cut out."""
    out = []
    for start, end, speed in segments:
        for k0, k1 in keep:
            a, b = max(start, k0), min(end, k1)
            if b - a > 0.02:
                out.append((a, b, speed))
    return sorted(out)


def fit_length(segments, target, max_factor=2.0):
    """Speed every segment up by one common factor (at most max_factor) so the cut lasts about `target`
    seconds. Returns (segments, factor); factor 1 when the cut is already short enough."""
    length = sum((e - s) / sp for s, e, sp in segments)
    if not target or length <= target:
        return segments, 1.0
    factor = min(length / target, max_factor)
    return [(s, e, sp * factor) for s, e, sp in segments], factor


def gap_segment(start, end, speedup, idle_threshold, max_idle_out):
    length = end - start
    if length < idle_threshold:
        return (start, end, 1.0)
    return (start, end, max(speedup, length / max_idle_out))


# ---- panel drawing -----------------------------------------------------------------------------


def clip_text(draw, text, face, width):
    if draw.textlength(text, font=face) <= width:
        return text
    while text and draw.textlength(text + "…", font=face) > width:
        text = text[:-1]
    return text + "…"


def wrap(draw, text, face, width, max_lines):
    lines = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=face) <= width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = clip_text(draw, lines[-1] + " …", face, width)
    return lines


def describe_args(args):
    parts = []
    for key, value in args.items():
        if isinstance(value, bool):
            parts.append(key if value else f"{key}=false")
        else:
            parts.append(str(value) if len(args) == 1 else f"{key}={value}")
    return "  ".join(parts)


def current_status(timeline, t):
    status = {}
    for when, sample in timeline.statuses:
        if when > t:
            break
        status.update(sample)
    if not status and timeline.statuses:
        # before the first sample the panel shows that sample rather than an empty block
        status = dict(timeline.statuses[0][1])
    return status


def panel_state(timeline, t, speed):
    """Everything the panel shows at source time t, as a hashable value so identical frames are drawn once."""
    done = [c for c in timeline.calls if c.end <= t]
    running = next((c for c in timeline.calls if c.start <= t < c.end), None)
    caption = next((text for start, end, text in timeline.captions if start <= t < end), None)
    status = dict(current_status(timeline, t))
    shown = [kind for start, end, kind, _ in timeline.alerts if start <= t and (end is None or t < end)]
    if shown:
        status["alerts"] = shown
    banner = next((text for start, end, text in reversed(timeline.banners) if start <= t < end), None)
    perf = next((values for when, values in reversed(timeline.metrics) if when <= t), None)
    if perf:
        status["perf"] = perf
    if banner:
        status["banner"] = banner
    recent = tuple((c.name, json.dumps(c.args, sort_keys=True), c.ok, c.summary or c.error, round(c.end - c.start, 1))
                   for c in done[-6:])
    return (recent, (running.name, json.dumps(running.args, sort_keys=True)) if running else None, caption,
            json.dumps(status, sort_keys=True), round(speed, 1))


def draw_frame(state, title):
    Image, ImageDraw, _ = need_pillow()
    recent, running, caption, status_json, speed = state
    status = json.loads(status_json)
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    # game label above the footage and caption bar below it
    draw.text((24, 6), "GAME", font=font("bold", 20), fill=DIM)
    draw.text((GAME_W - 24, 6), title, font=font("sans", 20), fill=DIM, anchor="ra")
    if speed > 1.05:
        # the footage is sped up: say so on the game itself, not only in the panel
        draw.rounded_rectangle((100, 2, 290, 34), radius=8, fill=(60, 45, 8))
        draw.text((195, 18), f">> x{speed:g} speed", font=font("bold", 20), fill=WARN, anchor="mm")
    bar_top = GAME_Y + GAME_H + 14
    banner = status.get("banner")
    draw.rounded_rectangle((16, bar_top, GAME_W - 16, H - 16), radius=14, fill=(70, 20, 24) if banner else CARD_BG,
                           outline=BAD if banner else None, width=3)
    if banner:
        draw.text((GAME_W - 40, bar_top + 18), clip_text(draw, banner, font("bold", 28), GAME_W - 360),
                  font=font("bold", 28), fill=BAD, anchor="ra")
    if caption:
        draw.text((40, bar_top + 18), "AGENT SAYS", font=font("bold", 20), fill=ACCENT)
        lines = wrap(draw, caption, font("sans", 34), GAME_W - 112, 4)
        for index, line in enumerate(lines):
            draw.text((40, bar_top + 56 + index * 46), line, font=font("sans", 34), fill=FG)
    else:
        draw.text((40, bar_top + 18), "AGENT SAYS", font=font("bold", 20), fill=DIM)
    # panel
    draw.rectangle((PANEL_X, 0, W, H), fill=PANEL_BG)
    x0, x1 = PANEL_X + 28, W - 28
    draw.text((x0, 22), "AGENT VIEW", font=font("bold", 30), fill=FG)
    label, color = ("calling a tool", WARN) if running else ("thinking", ACCENT)
    draw.ellipse((x1 - 190, 34, x1 - 174, 50), fill=color)
    draw.text((x1 - 162, 28), label, font=font("sans", 22), fill=color)
    if speed > 1.05:
        draw.rounded_rectangle((x0, 66, x0 + 150, 98), radius=8, fill=(60, 45, 8))
        draw.text((x0 + 14, 70), f"FAST x{speed:g}", font=font("bold", 22), fill=WARN)
    draw.text((x0, 112), "TOOL CALLS", font=font("bold", 20), fill=DIM)
    rows = [(n, json.loads(a), ok, text, took, False) for n, a, ok, text, took in recent]
    if running:
        rows.append((running[0], json.loads(running[1]), True, "running…", 0, True))
    perf = status.get("perf")
    rows = rows[-3:] if perf else rows[-6:]
    y = 144
    name_face, small = font("mono", 24), font("mono", 19)
    for name, args, ok, text, took, live in rows:
        draw.rounded_rectangle((x0 - 8, y - 6, x1 + 8, y + 94), radius=10, fill=(32, 40, 54) if live else (26, 32, 44))
        mark_color = WARN if live else (GOOD if ok else BAD)
        draw.ellipse((x0 + 2, y + 7, x0 + 18, y + 23), fill=mark_color)
        draw.text((x0 + 30, y), name, font=name_face, fill=ACCENT)
        if took and not live:
            draw.text((x1, y + 4), f"{took:g}s", font=small, fill=DIM, anchor="ra")
        args_text = args if isinstance(args, str) else describe_args(args) or "no arguments"
        draw.text((x0 + 30, y + 32), clip_text(draw, args_text, small, x1 - x0 - 30), font=small, fill=DIM)
        draw.text((x0 + 30, y + 58), clip_text(draw, text, small, x1 - x0 - 30), font=small, fill=FG if ok else BAD)
        y += 108
    if perf:
        draw_perf(draw, perf, x0, x1, 144 + 3 * 108 + 4, 144 + 6 * 108 - 4)
    # status block
    top = 144 + 6 * 108 + 8
    draw.text((x0, top), "STATUS", font=font("bold", 20), fill=DIM)
    top += 32
    lines = []
    quest = status.get("quest")
    if quest:
        lines.append(("Quest", f"{quest.get('name')}  (stage {quest.get('stage')})"))
        if quest.get("objective"):
            lines.append(("Goal", quest["objective"]))
    else:
        lines.append(("Quest", "none tracked yet"))
    health = status.get("health")
    lines.append(("Health", f"{health[0]:g} / {health[1]:g}" if health and health[1] else "unknown"))
    lines.append(("Location", str(status["cell"]) if status.get("cell") else "unknown"))
    state_flags = [name for name, on in (("in combat", status.get("inCombat")), ("walking", status.get("walking")),
                                         ("sneaking", status.get("sneaking"))) if on]
    if state_flags:
        lines.append(("State", ", ".join(state_flags)))
    if status.get("alerts"):
        lines.append(("Alert", ", ".join(status["alerts"])))
    for key, value in lines:
        draw.text((x0, top), key, font=font("sans", 20), fill=DIM)
        for index, text in enumerate(wrap(draw, value, font("sans", 22), x1 - x0 - 110, 2)):
            draw.text((x0 + 110, top - 2 + index * 26), text, font=font("sans", 22), fill=FG)
            if index:
                top += 26
        top += 32
    if health and health[1]:
        bar_y = H - 34
        draw.rounded_rectangle((x0, bar_y, x1, bar_y + 10), radius=5, fill=(40, 48, 62))
        draw.rounded_rectangle((x0, bar_y, x0 + int((x1 - x0) * max(0.0, min(1.0, health[0] / health[1]))), bar_y + 10), radius=5, fill=GOOD)
    return image


def draw_perf(draw, perf, x0, x1, top, bottom):
    """A live metrics box (for example a profiler snapshot): label/value rows, a sparkline and a badge."""
    draw.rounded_rectangle((x0 - 8, top, x1 + 8, bottom), radius=12, fill=(18, 30, 40), outline=ACCENT, width=2)
    draw.text((x0 + 6, top + 12), perf.get("title", "METRICS"), font=font("bold", 22), fill=ACCENT)
    badge = perf.get("badge")
    if badge:
        face = font("bold", 20)
        width = draw.textlength(badge, font=face) + 28
        color = BAD if "gpu" in badge.lower() else GOOD if "cpu" in badge.lower() else WARN
        draw.rounded_rectangle((x1 - width, top + 10, x1, top + 40), radius=8, fill=(40, 30, 20), outline=color, width=2)
        draw.text((x1 - width / 2, top + 25), badge, font=face, fill=color, anchor="mm")
    rows = perf.get("rows") or []
    spark = [float(v) for v in perf.get("spark") or [] if v is not None]
    spark_h = 70 if len(spark) > 1 else 0
    y = top + 54
    column = (x1 - x0) // 2
    face, value_face = font("sans", 19), font("mono", 22)
    for index, row in enumerate(rows[:12]):
        label, value = row[0], row[1]
        cx = x0 + 6 + (index % 2) * column
        cy = y + (index // 2) * 34
        if cy + 30 > bottom - spark_h - 12:
            break
        draw.text((cx, cy + 2), str(label), font=face, fill=DIM)
        draw.text((cx + column - 20, cy), clip_text(draw, str(value), value_face, column - 120), font=value_face,
                  fill=FG, anchor="ra")
    if spark_h:
        sx0, sx1, sy1 = x0 + 6, x1 - 6, bottom - 12
        sy0 = sy1 - spark_h
        draw.rectangle((sx0, sy0, sx1, sy1), fill=(12, 20, 28))
        high = max(max(spark), 1e-6)
        step = (sx1 - sx0) / max(len(spark) - 1, 1)
        points = [(sx0 + i * step, sy1 - (v / high) * (spark_h - 6)) for i, v in enumerate(spark)]
        draw.line(points, fill=GOOD, width=2)
        draw.text((sx0 + 6, sy0 + 2), perf.get("sparkLabel", ""), font=font("sans", 16), fill=DIM)


def draw_slide(spec, path):
    """A full-screen still: a title, an image fitted below it and a caption line."""
    Image, ImageDraw, _ = need_pillow()
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.text((60, 34), spec.get("title", ""), font=font("bold", 44), fill=FG)
    if spec.get("tag"):
        draw.text((W - 60, 44), spec["tag"], font=font("bold", 24), fill=ACCENT, anchor="ra")
    caption = spec.get("caption")
    box = (60, 110, W - 60, H - (110 if caption else 40))
    picture = Image.open(spec["image"]).convert("RGB")
    scale = min((box[2] - box[0]) / picture.width, (box[3] - box[1]) / picture.height)
    size = (max(1, int(picture.width * scale)), max(1, int(picture.height * scale)))
    picture = picture.resize(size, Image.LANCZOS)
    left = box[0] + ((box[2] - box[0]) - size[0]) // 2
    draw.rectangle((left - 3, box[1] - 3, left + size[0] + 2, box[1] + size[1] + 2), outline=ACCENT, width=3)
    image.paste(picture, (left, box[1]))
    if caption:
        for index, line in enumerate(wrap(draw, caption, font("sans", 30), W - 120, 2)):
            draw.text((60, H - 96 + index * 40), line, font=font("sans", 30), fill=DIM)
    image.save(path)


INSET_MAX = (600, 460)


def draw_inset(image_path, label, path):
    """Picture-in-picture still with a border and a label strip; returns its size."""
    Image, ImageDraw, _ = need_pillow()
    picture = Image.open(image_path).convert("RGB")
    picture.thumbnail(INSET_MAX, Image.LANCZOS)
    strip = 34 if label else 0
    out = Image.new("RGB", (picture.width + 8, picture.height + 8 + strip), ACCENT)
    draw = ImageDraw.Draw(out)
    if label:
        draw.rectangle((0, 0, out.width, strip), fill=(20, 40, 70))
        draw.text((12, strip // 2), clip_text(draw, label, font("bold", 20), out.width - 24), font=font("bold", 20),
                  fill=FG, anchor="lm")
    out.paste(picture, (4, strip + 4))
    out.save(path)
    return out.size


def draw_card(lines, path):
    Image, ImageDraw, _ = need_pillow()
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    y = H // 2 - 40 * len(lines)
    for index, (text, size, color, kind) in enumerate(lines):
        face = font(kind, size)
        for line in wrap(draw, text, face, W - 360, 3):
            draw.text((W // 2, y), line, font=face, fill=color, anchor="mm")
            y += int(size * 1.35)
        y += 14
    image.save(path)


# ---- ffmpeg ------------------------------------------------------------------------------------


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def panel_boundaries(timeline, duration, segments):
    times = {0.0, duration}
    for call in timeline.calls:
        times.update((call.start, call.end))
    for start, end, _ in timeline.captions:
        times.update((start, end))
    for start, end, _ in timeline.banners:
        times.update((start, end))
    times.update(t for t, _ in timeline.statuses)
    times.update(t for t, _ in timeline.metrics)
    for start, end, _ in segments:
        times.update((start, end))
    return sorted(t for t in times if 0 <= t <= duration)


def speed_at(segments, t):
    for start, end, speed in segments:
        if start <= t < end:
            return speed
    return 1.0


def build_panel_images(timeline, duration, segments, title, workdir):
    times = panel_boundaries(timeline, duration, segments)
    cache, entries = {}, []
    for index, start in enumerate(times[:-1]):
        length = times[index + 1] - start
        if length <= 1e-3:
            continue
        if not any(a <= start + 1e-4 < b for a, b, _ in segments):
            # cut out of the result: one cheap still keeps the panel stream aligned with the footage
            if "cut" not in cache:
                cache["cut"] = workdir / "panel-cut.png"
                need_pillow()[0].new("RGB", (W, H), BG).save(cache["cut"])
            entries.append((cache["cut"], length))
            continue
        # sample just after the boundary so a call that starts here is already shown as running
        state = panel_state(timeline, start + 1e-4, speed_at(segments, start + 1e-4))
        if state not in cache:
            cache[state] = workdir / f"panel-{len(cache):04d}.png"
            draw_frame(state, title).save(cache[state])
        entries.append((cache[state], length))
    return entries


def write_concat(entries, path):
    lines = []
    for image, length in entries:
        lines += [f"file '{image}'", f"duration {length:.4f}"]
    lines.append(f"file '{entries[-1][0]}'")
    path.write_text("\n".join(lines) + "\n")


def split_at(segments, times):
    """Cut segments at the given source times, so a slide can be inserted exactly there."""
    out = []
    for start, end, speed in segments:
        cuts = sorted(t for t in times if start + 0.05 < t < end - 0.05)
        for cut in cuts:
            out.append((start, cut, speed))
            start = cut
        out.append((start, end, speed))
    return out


def slide_positions(segments, slides):
    """For each slide time, the number of segments shown before it (a slide in a cut stretch goes before the next one)."""
    return [sum(1 for start, _, _ in segments if start < t - 0.05) for t in slides]


def filter_script(segments, intro, outro, fade=0.0, insets=(), slides=()):
    """One graph: overlay footage on the panel video, cut the speed segments, join with the cards.

    insets: (start, end, x, y) in source time, one image input each after the cards;
    slides: (position, duration) with position from slide_positions, one image input each after the insets.
    """
    parts = [f"[0:v]fps={FPS},format=yuv420p,setpts=PTS-STARTPTS[panel]",
             f"[1:v]setpts=PTS-STARTPTS,fps={FPS},scale={GAME_W}:{GAME_H}:force_original_aspect_ratio=decrease,"
             f"pad={GAME_W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2:color=black[game]",
             f"[panel][game]overlay={GAME_X}:{GAME_Y}:eof_action=pass,format=yuv420p[comp0]"]
    index = 2 + bool(intro) + bool(outro)
    for k, (start, end, x, y) in enumerate(insets):
        # the inset is placed in source time, before the speed segments are cut, so it speeds up with the footage
        parts.append(f"[{index}:v]format=yuv420p[ins{k}]")
        parts.append(f"[comp{k}][ins{k}]overlay={x}:{y}:eof_action=pass:enable='between(t,{start:.3f},{end:.3f})'"
                     f"[comp{k + 1}]")
        index += 1
    parts.append(f"[comp{len(insets)}]null[comp]")
    count = len(segments)
    parts.append(f"[comp]split={count}" + "".join(f"[c{i}]" for i in range(count)))
    labels = []
    card = 2
    if intro:
        parts.append(f"[{card}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,fade=t=out:st={intro - 0.4}:d=0.4[intro]")
        labels.append("[intro]")
        card += 1
    slide_labels = {}
    for j, (position, length) in enumerate(slides):
        parts.append(f"[{index}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,fade=t=in:st=0:d=0.3,"
                     f"fade=t=out:st={max(length - 0.3, 0):.3f}:d=0.3,trim=duration={length:.3f}[sl{j}]")
        slide_labels.setdefault(position, []).append(f"[sl{j}]")
        index += 1
    for i, (start, end, speed) in enumerate(segments):
        labels += slide_labels.pop(i, [])
        # a short fade marks a jump in a highlight cut, where the footage skips ahead
        cut_in = fade and i and abs(segments[i - 1][1] - start) > 0.05
        cut_out = fade and i + 1 < count and abs(segments[i + 1][0] - end) > 0.05
        out_len = (end - start) / speed
        fades = (f",fade=t=in:st=0:d={fade}" if cut_in else "") + (
            f",fade=t=out:st={max(out_len - fade, 0):.4f}:d={fade}" if cut_out else "")
        parts.append(f"[c{i}]trim=start={start:.4f}:end={end:.4f},setpts=(PTS-STARTPTS)/{speed:.4f},fps={FPS}{fades}[s{i}]")
        labels.append(f"[s{i}]")
    for rest in slide_labels.values():
        labels += rest
    if outro:
        parts.append(f"[{card}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,fade=t=in:st=0:d=0.4[outro]")
        labels.append("[outro]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0,format=yuv420p[out]")
    return ";\n".join(parts)


def compose(options):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("skyrim-devtools-video: ffmpeg/ffprobe not found (brew install ffmpeg)")
    footage = Path(options.footage)
    start_ms = options.footage_start_ms
    sidecar = footage.with_suffix(".capture.json")
    if start_ms is None and options.use_sidecar and sidecar.exists():
        start_ms = json.loads(sidecar.read_text()).get("startEpochMs")
    timeline = load_timeline(options.timeline, start_ms)
    duration = getattr(options, "duration", None) or (timeline.footage_seconds if getattr(options, "emit_dir", None)
                                                       else None) or probe_duration(footage)
    segments = plan_segments(timeline, duration, options.speedup_idle, options.idle_threshold, options.max_idle,
                             options.speedup_walk, options.speedup_action)
    keep = [list(parse_interval(k)) for k in getattr(options, "keep", None) or []]
    if getattr(options, "highlights", False):
        keep += narration_windows(timeline, options.highlight_before, options.highlight_after)
    skip = [parse_interval(k) for k in getattr(options, "skip", None) or []]
    if keep or skip:
        segments = restrict(segments, subtract(merge(keep) if keep else [[0.0, duration]], skip))
        if not segments:
            raise SystemExit("nothing left to show: the kept windows are empty")
    segments, factor = fit_length(segments, getattr(options, "target_length", None))
    slides = [item for item in timeline.slides if 0 <= item[0] <= duration]
    segments = split_at(segments, [t for t, _, _ in slides])
    out_seconds = sum((e - s) / sp for s, e, sp in segments) + sum(length for _, length, _ in slides)
    print(f"footage {duration:.1f}s, {len(timeline.calls)} tool calls, {len(timeline.captions)} captions, "
          f"{len(segments)} segments -> {out_seconds:.1f}s" + (f" (all sped up x{factor:.2f} to fit)" if factor > 1 else ""))
    if options.dry_run:
        for s, e, sp in segments:
            print(f"  {s:7.1f}-{e:7.1f}  x{sp:g}")
        for t, length, spec in slides:
            print(f"  {t:7.1f}  {spec['kind']} {length:g}s: {spec.get('title', '')}")
        return 0
    emit = getattr(options, "emit_dir", None)
    if emit:
        Path(emit).mkdir(parents=True, exist_ok=True)
    workdir = Path(emit) if emit else Path(tempfile.mkdtemp(prefix="skyrim-video-"))
    if emit:
        workdir = workdir.resolve()
    try:
        entries = build_panel_images(timeline, duration, segments, options.title or "", workdir)
        concat = workdir / "panel.txt"
        write_concat(entries, concat)
        cards = []
        intro = outro = 0
        if not options.no_cards:
            intro, outro = 3.5, 4.0
            draw_card([(options.title or "An AI agent plays Skyrim", 78, FG, "bold"),
                       (options.subtitle, 36, DIM, "sans")], workdir / "intro.png")
            calls = len(timeline.calls)
            draw_card([("Played entirely through tool calls", 62, FG, "bold"),
                       (f"{calls} tool calls, {len(timeline.captions)} comments, no human input", 36, DIM, "sans"),
                       ("skyrim-use: automated playtesting for Skyrim mods", 28, ACCENT, "sans")], workdir / "outro.png")
            cards = [("intro.png", intro), ("outro.png", outro)]
        cards = [card for card in cards if card[1]]
        insets = []
        for k, (start, end, image, label) in enumerate(timeline.insets):
            if start >= duration:
                continue
            width, _ = draw_inset(image, label, workdir / f"inset-{k:03d}.png")
            # top right corner of the game picture, clear of the speed badge row
            insets.append((start, min(end, duration), GAME_X + GAME_W - width - 18, GAME_Y + 18))
            cards.append((f"inset-{k:03d}.png", duration))
        for j, (t, length, spec) in enumerate(slides):
            name = f"slide-{j:03d}.png"
            if spec["kind"] == "chapter":
                lines = [(spec.get("kicker", ""), 30, ACCENT, "bold")] if spec.get("kicker") else []
                draw_card(lines + [(spec.get("title", ""), 84, FG, "bold"), (spec.get("subtitle", ""), 36, DIM, "sans")],
                          workdir / name)
            else:
                draw_slide(spec, workdir / name)
            cards.append((name, length))
        positions = slide_positions(segments, [t for t, _, _ in slides])
        script = workdir / "graph.txt"
        cut = bool(keep or skip)
        script.write_text(filter_script(segments, intro, outro, fade=0.25 if cut else 0.0,
                                        insets=[(s, e, x, y) for s, e, x, y in insets],
                                        slides=list(zip(positions, [length for _, length, _ in slides]))))
        if emit:
            # the concat list and graph name their files relative to the folder, so it can be copied to
            # the host that holds the footage and run there
            write_concat([(Path(image).name, length) for image, length in entries], concat)
        local = (lambda name: name) if emit else (lambda name: str(workdir / name))
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
                   "-f", "concat", "-safe", "0", "-i", local("panel.txt"), "-i", str(footage)]
        for name, length in cards:
            command += ["-loop", "1", "-t", f"{length}", "-framerate", str(FPS), "-i", local(name)]
        command += ["-/filter_complex", local("graph.txt"), "-map", "[out]", "-c:v", options.encoder,
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(options.output)]
        if options.encoder == "h264_videotoolbox":
            command = [c for c in command if c not in ("-preset", "veryfast", "-crf", "20")]
            command[command.index(str(options.output)):command.index(str(options.output))] = ["-b:v", "10M"]
        if emit:
            (workdir / "command.json").write_text(json.dumps(command, indent=1))
            print(f"wrote the ffmpeg inputs to {workdir}; run command.json from inside that folder")
            return 0
        result = subprocess.run(command)
        if result.returncode:
            raise SystemExit(f"ffmpeg failed with code {result.returncode} (work files kept in {workdir})")
    except BaseException:
        if options.keep_work or emit:
            print(f"work files kept in {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        raise
    if options.keep_work:
        print(f"work files in {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"wrote {options.output}")
    return 0


def compose_parts(options):
    """Compose one video from several recording sessions: each part is footage:timeline, cards go around the whole."""
    import copy

    workdir = Path(tempfile.mkdtemp(prefix="skyrim-video-parts-"))
    pieces = []
    total_calls = total_captions = 0
    try:
        for number, spec in enumerate(options.part):
            footage, _, timeline_path = spec.partition(":")
            if not timeline_path:
                raise SystemExit("--part is footage.mp4:timeline.jsonl")
            part = copy.copy(options)
            part.footage, part.timeline, part.no_cards, part.dry_run = footage, timeline_path, True, False
            part.output = workdir / f"part{number}.mp4"
            sidecar = Path(footage).with_suffix(".capture.json")
            part.footage_start_ms = json.loads(sidecar.read_text()).get("startEpochMs") if sidecar.exists() else None
            timeline = load_timeline(timeline_path, part.footage_start_ms)
            total_calls += len(timeline.calls)
            total_captions += len(timeline.captions)
            compose(part)
            pieces.append(part.output)
        intro_card, outro_card = workdir / "intro.png", workdir / "outro.png"
        draw_card([(options.title or "An AI agent plays Skyrim", 78, FG, "bold"), (options.subtitle, 36, DIM, "sans")], intro_card)
        draw_card([("Played entirely through tool calls", 62, FG, "bold"),
                   (f"{total_calls} tool calls, {total_captions} comments, no human input", 36, DIM, "sans"),
                   ("skyrim-use: automated playtesting for Skyrim mods", 28, ACCENT, "sans")], outro_card)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        command += ["-loop", "1", "-t", "3.5", "-framerate", str(FPS), "-i", str(intro_card)]
        for piece in pieces:
            command += ["-i", str(piece)]
        command += ["-loop", "1", "-t", "4", "-framerate", str(FPS), "-i", str(outro_card)]
        count = len(pieces) + 2
        chain = ";".join(f"[{i}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p[v{i}]" for i in range(count))
        chain += ";" + "".join(f"[v{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0[out]"
        command += ["-filter_complex", chain, "-map", "[out]", "-c:v", options.encoder, "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(options.output)]
        if subprocess.run(command).returncode:
            raise SystemExit("ffmpeg failed while joining the parts")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"wrote {options.output}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-video", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compose", help="footage + timeline -> showcase video")
    c.add_argument("--footage", help="MP4 from capture_stop")
    c.add_argument("--timeline", help="session timeline JSONL")
    c.add_argument("--part", action="append", help="footage.mp4:timeline.jsonl; repeat for a recording split over sessions")
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--speedup-idle", type=float, default=4.0, help="speed factor for stretches with nothing to watch")
    c.add_argument("--speedup-walk", type=float, default=None,
                   help="speed factor for the middle of long walks and waits (nav_*, walk, wait_for); default: same as --speedup-idle")
    c.add_argument("--speedup-action", type=float, default=1.0,
                   help="speed factor for tool activity with no caption or conversation on screen (fights, searching)")
    c.add_argument("--idle-threshold", type=float, default=3.0, help="only gaps at least this long (s) are sped up")
    c.add_argument("--max-idle", type=float, default=6.0, help="no sped-up stretch lasts longer than this (s) in the result")
    c.add_argument("--title")
    c.add_argument("--subtitle", default="")
    c.add_argument("--footage-start-ms", type=int, help="UTC epoch ms of the first footage frame (default: from the timeline)")
    c.add_argument("--no-cards", action="store_true", help="skip the intro and outro cards")
    c.add_argument("--dry-run", action="store_true", help="print the speed plan only")
    c.add_argument("--keep-work", action="store_true")
    c.add_argument("--encoder", default="libx264", help="ffmpeg video encoder (h264_videotoolbox is faster on a Mac)")
    c.add_argument("--no-sidecar", dest="use_sidecar", action="store_false")
    c.add_argument("--keep", action="append", metavar="START-END",
                   help="only show this stretch of footage (seconds or M:SS); repeat for several")
    c.add_argument("--highlights", action="store_true",
                   help="only show the stretches around narration markers (adds to --keep)")
    c.add_argument("--highlight-before", type=float, default=4.0, help="seconds kept before each narration marker")
    c.add_argument("--highlight-after", type=float, default=6.0, help="seconds kept after each caption ends")
    c.add_argument("--skip", action="append", metavar="START-END", help="cut this stretch out; repeat for several")
    c.add_argument("--target-length", type=float, help="speed the whole cut up (at most 2x) to last about this many seconds")
    c.add_argument("--duration", type=float, help="footage length in seconds (default: ffprobe, or the capture stop record with --emit-dir)")
    c.add_argument("--emit-dir", help="write panel stills, cards, graph and command.json here instead of running ffmpeg, "
                   "to run on the host that holds the footage; --footage is then that host's path")
    options = parser.parse_args(argv)
    if options.part:
        return compose_parts(options)
    if not options.footage or not options.timeline:
        parser.error("pass --footage and --timeline, or one or more --part footage:timeline")
    return compose(options)


if __name__ == "__main__":
    sys.exit(main())
