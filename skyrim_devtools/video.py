"""Compose a showcase video: game footage beside an "agent view" panel, boring stretches sped up.

  skyrim-devtools-video compose --footage x.mp4 --timeline t.jsonl -o out.mp4 [--speedup-idle 4] [--title ...]

The panel (recent tool calls, status, captions) is drawn with Pillow as one still image per change in
source time, turned into a video, and overlaid with the footage in a single ffmpeg run that also applies
the speed-up segments and the title cards. Needs `pip install -e .[video]` and ffmpeg on PATH.
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
BORING_TOOLS = ("wait_for", "game_start", "load_save", "reload", "center_on_cell", "nav_", "walk", "travel", "capture_")
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
    footage_start_ms: int | None = None


def load_timeline(path, footage_start_ms=None):
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if footage_start_ms is None:
        starts = [r["startEpochMs"] for r in records if r.get("t") == "capture" and r.get("action") == "start"]
        if not starts:
            raise SystemExit("no capture start in the timeline: pass --footage-start-ms")
        footage_start_ms = starts[-1]
    t0 = footage_start_ms
    timeline = Timeline(footage_start_ms=t0)
    narrations = []
    for record in records:
        kind = record.get("t")
        if kind == "tool":
            timeline.calls.append(Call(record["name"], record.get("args", {}), (record["startMs"] - t0) / 1000,
                                       (record["endMs"] - t0) / 1000, record.get("ok", True),
                                       record.get("summary", ""), record.get("error", "")))
        elif kind == "narrate":
            narrations.append(((record["ms"] - t0) / 1000, record["text"]))
        elif kind == "status":
            timeline.statuses.append(((record["ms"] - t0) / 1000, {k: v for k, v in record.items() if k not in ("t", "ms")}))
    narrations.sort()
    for index, (start, text) in enumerate(narrations):
        hold = max(4.0, 0.07 * len(text) + 1.5)
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


def plan_segments(timeline, duration, speedup=4.0, idle_threshold=3.0, max_idle_out=6.0):
    """Return [(start, end, speed)] over source time covering [0, duration]; gaps with nothing to watch run faster."""
    windows = []
    for call in timeline.calls:
        if call.end < 0 or call.start > duration:
            continue
        if is_dialogue(call.name):
            windows.append((call.start - 1.0, call.end + 2.0))
        elif is_boring(call.name):
            # the moment of the call and of its result stay at normal speed; the middle is the wait
            windows.append((call.start - 0.5, call.start + 1.0))
            windows.append((call.end - 0.5, call.end + 1.0))
        else:
            windows.append((call.start - 0.5, call.end + 1.5))
    for start, end, _ in timeline.captions:
        windows.append((start - 0.3, end))
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
    return [s for s in segments if s[1] - s[0] > 0.02]


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
    status = current_status(timeline, t)
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
    bar_top = GAME_Y + GAME_H + 14
    draw.rounded_rectangle((16, bar_top, GAME_W - 16, H - 16), radius=14, fill=CARD_BG)
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
    rows = rows[-6:]
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
    times.update(t for t, _ in timeline.statuses)
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


def filter_script(segments, intro, outro):
    """One graph: overlay footage on the panel video, cut the speed segments, join with the cards."""
    parts = [f"[0:v]fps={FPS},format=yuv420p,setpts=PTS-STARTPTS[panel]",
             f"[1:v]setpts=PTS-STARTPTS,fps={FPS},scale={GAME_W}:{GAME_H}:force_original_aspect_ratio=decrease,"
             f"pad={GAME_W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2:color=black[game]",
             f"[panel][game]overlay={GAME_X}:{GAME_Y}:eof_action=pass,format=yuv420p[comp]"]
    count = len(segments)
    parts.append(f"[comp]split={count}" + "".join(f"[c{i}]" for i in range(count)))
    labels = []
    index = 2
    if intro:
        parts.append(f"[{index}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,fade=t=out:st={intro - 0.4}:d=0.4[intro]")
        labels.append("[intro]")
        index += 1
    for i, (start, end, speed) in enumerate(segments):
        parts.append(f"[c{i}]trim=start={start:.4f}:end={end:.4f},setpts=(PTS-STARTPTS)/{speed:.4f},fps={FPS}[s{i}]")
        labels.append(f"[s{i}]")
    if outro:
        parts.append(f"[{index}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,fade=t=in:st=0:d=0.4[outro]")
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
    duration = probe_duration(footage)
    segments = plan_segments(timeline, duration, options.speedup_idle, options.idle_threshold, options.max_idle)
    out_seconds = sum((e - s) / sp for s, e, sp in segments)
    print(f"footage {duration:.1f}s, {len(timeline.calls)} tool calls, {len(timeline.captions)} captions, "
          f"{len(segments)} segments -> {out_seconds:.1f}s")
    if options.dry_run:
        for s, e, sp in segments:
            print(f"  {s:7.1f}-{e:7.1f}  x{sp:g}")
        return 0
    workdir = Path(tempfile.mkdtemp(prefix="skyrim-video-"))
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
        script = workdir / "graph.txt"
        script.write_text(filter_script(segments, intro, outro))
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(footage)]
        for name, length in cards:
            command += ["-loop", "1", "-t", f"{length}", "-framerate", str(FPS), "-i", str(workdir / name)]
        command += ["-filter_complex_script", str(script), "-map", "[out]", "-c:v", options.encoder,
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(options.output)]
        if options.encoder == "h264_videotoolbox":
            command = [c for c in command if c not in ("-preset", "veryfast", "-crf", "20")]
            command[command.index(str(options.output)):command.index(str(options.output))] = ["-b:v", "10M"]
        result = subprocess.run(command)
        if result.returncode:
            raise SystemExit(f"ffmpeg failed with code {result.returncode} (work files kept in {workdir})")
    except BaseException:
        if options.keep_work:
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
    options = parser.parse_args(argv)
    if options.part:
        return compose_parts(options)
    if not options.footage or not options.timeline:
        parser.error("pass --footage and --timeline, or one or more --part footage:timeline")
    return compose(options)


if __name__ == "__main__":
    sys.exit(main())
