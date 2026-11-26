"""Event trace of a profile: decoding, Perfetto export, Chrome JSON fallback and a per-frame summary.

The plugin records engine function ranges, D3D11 calls (or per-pass batches of them), OS waits and file reads
into per-thread buffers on the same QPC clock as the samples (native/trace*.cpp). This module turns that,
plus the sampled stacks, GPU passes, counters and markers already in the profile, into one Perfetto trace
(protobuf TrackEvent, written by hand so no protobuf package is needed) to open in ui.perfetto.dev or query
with trace_processor. See docs/profiling.md, Perfetto trace.
"""

import base64
import bisect
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct

from .profile_gpu import board_series, counter_series, gpu_frames

EVENT = struct.Struct("<qIIQQ")
NAME_BITS = 20
NAME_MASK = (1 << NAME_BITS) - 1
FILE_CATEGORIES = ("other", "archive", "plugin", "save", "ini", "dll", "texture", "mesh", "sound", "script")

# Engine hooks with public Address Library ids (the AE ids CommonLibSSE-NG ships for these functions). Anything
# else comes from a local hook list resolved by name against the local symbol tables; see local_hooks().
DEFAULT_HOOKS = [
    {"label": "Main::RenderWorld", "category": "render", "id": 107142},
    {"label": "Renderer::SubmitAccumulator", "category": "render", "id": 106436},
    {"label": "BGSSaveLoadManager::Save", "category": "save", "id": 35727},
    {"label": "BGSSaveLoadManager::Load", "category": "save", "id": 35728},
    {"label": "TES::SetCurrentCell", "category": "cell", "id": 13316},
    {"label": "TES::PurgeBufferedCells", "category": "cell", "id": 13299},
    {"label": "PlayerCharacter::CenterOnCell", "category": "cell", "id": 40437},
    {"label": "BSResourceNiBinaryStream", "category": "io", "id": 71014, "minUs": 50},
]


# ---- decoding ----------------------------------------------------------------------------------


def decode(raw):
    """The trace in a profile as {names: [(name, category)], threads: [{tid, name, events, dropped, shortWaits}],
    ...}; events are (startMs, ms, nameId, flags, a, b) on the profile's clock. None without a trace."""
    trace = raw.get("trace")
    if not trace:
        return None
    freq = float(trace.get("freq") or 1)
    origin = trace.get("originTicks") or 0
    names = [tuple(n) for n in trace.get("names", [])]
    threads = []
    for thread in trace.get("threads", []):
        blob = base64.b64decode(thread.get("events") or "")
        events = []
        for start, duration, word, a, b in EVENT.iter_unpack(blob):
            events.append(((start - origin) * 1000.0 / freq, duration * 1000.0 / freq, word & NAME_MASK, word >> NAME_BITS, a, b))
        events.sort(key=lambda e: (e[0], -e[1]))
        threads.append({"tid": thread.get("tid"), "name": thread.get("name") or "",
                        "nameSource": thread.get("nameSource") or "", "events": events,
                        "dropped": thread.get("dropped", 0), "shortWaits": thread.get("shortWaits", 0)})
    return {"names": names, "threads": threads, "settings": trace.get("settings", {}), "hooks": trace.get("hooks", []),
            "api": trace.get("api", {}), "osHooks": trace.get("osHooks", {}), "freq": freq}


def event_args(category, name, flags, a, b):
    """The a/b words of an event as named arguments."""
    if category == "pass":
        return {"pass_index": flags, "draws": a & 0xFFFFFFFF, "maps": a >> 32, "dispatches": b & 0xFFFF,
                "other_calls": (b >> 16) & 0xFFFF, "api_us": b >> 32}
    if category == "wait":
        return {"timeout_ms": None if a == 0xFFFFFFFF else a, "result": b}
    if category == "io":
        return {"bytes": a, "file_category": FILE_CATEGORIES[b & 0xFF] if (b & 0xFF) < len(FILE_CATEGORIES) else b & 0xFF,
                "overlapped": bool(b & 0x100)}
    if category == "d3d11":
        if name == "Present":
            return {"frame": a}
        if name == "Dispatch":
            return {"x": a & 0x1FFFFF, "y": (a >> 21) & 0x1FFFFF, "z": a >> 42}
        if name.startswith("Draw"):
            return {"count": a, "instances": b}
        if name in ("Map", "UpdateSubresource"):
            return {"bytes": a, "map_type": b} if name == "Map" else {"bytes": a}
        return {"a": a, "b": b} if a or b else {}
    return {}


def pass_frames(events, names):
    """Frame number of each pass event on the render thread: passes after Present(frame n) belong to n + 1."""
    frame = None
    out = {}
    for index, (start, ms, name_id, flags, a, b) in enumerate(events):
        name, category = names[name_id] if name_id < len(names) else (f"#{name_id}", "")
        if category == "d3d11" and name == "Present":
            frame = a + 1
        elif category == "pass" and frame is not None:
            out[index] = frame
    return out


# ---- protobuf encoding -------------------------------------------------------------------------


def _varint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field, wire):
    return _varint((field << 3) | wire)


def _uint(field, value):
    return _key(field, 0) + _varint(int(value))


def _sint(field, value):
    return _key(field, 0) + _varint(int(value) & ((1 << 64) - 1))


def _double(field, value):
    return _key(field, 1) + struct.pack("<d", float(value))


def _fixed64(field, value):
    return _key(field, 1) + struct.pack("<Q", int(value) & ((1 << 64) - 1))


def _bytes(field, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _key(field, 2) + _varint(len(data)) + data


def _repeated(field, values):
    # Perfetto's protos are proto2 without [packed=true], and its decoder reads only the unpacked encoding.
    return b"".join(_sint(field, v) for v in values)


# TracePacket, TrackDescriptor, TrackEvent and profiling field numbers (protos/perfetto/trace/*).
PK_TIMESTAMP, PK_SEQ, PK_TRACK_EVENT, PK_INTERNED, PK_SEQ_FLAGS = 8, 10, 11, 12, 13
PK_THREAD_DESCRIPTOR, PK_STREAMING_PROFILE, PK_TRACK_DESCRIPTOR = 44, 54, 60
SEQ_CLEARED, SEQ_NEEDS = 1, 2
TE_BEGIN, TE_END, TE_INSTANT, TE_COUNTER = 1, 2, 3, 4


def _uuid(*parts):
    digest = hashlib.blake2b("\x1f".join(str(p) for p in parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little") >> 1


def _annotation(name, value):
    body = _bytes(10, name)
    if isinstance(value, bool):
        body += _uint(2, int(value))
    elif isinstance(value, int):
        body += _uint(3, value) if value >= 0 else _sint(4, value)
    elif isinstance(value, float):
        body += _double(5, value)
    else:
        body += _bytes(6, "" if value is None else str(value))
    return _bytes(4, body)


class PerfettoWriter:
    """Builds a Perfetto trace: tracks, slices, counters and flows on one sequence, samples on one sequence per
    thread. Slices are written as begin/end pairs sorted by time; nest() keeps each track properly nested."""

    TRACK_SEQ = 1

    def __init__(self, pid):
        self.pid = pid
        self.descriptors = []
        self.events = []  # (ts, order, depth, bytes)
        self.sample_packets = []
        self.flows = 0

    def packet(self, body, ts=None, seq=None, flags=None):
        out = b""
        if ts is not None:
            out += _uint(PK_TIMESTAMP, ts)
        out += _uint(PK_SEQ, seq or self.TRACK_SEQ)
        if flags is not None:
            out += _uint(PK_SEQ_FLAGS, flags)
        return _bytes(1, out + body)

    def track(self, uuid, name=None, parent=None, thread=None, process=None, counter=False, rank=None, ordering=None):
        body = _uint(1, uuid)
        if name is not None:
            body += _bytes(2, name)
        if process is not None:
            body += _bytes(3, _uint(1, process[0]) + _bytes(6, process[1]))
        if thread is not None:
            body += _bytes(4, _uint(1, thread[0]) + _uint(2, thread[1]) + (_bytes(5, thread[2]) if thread[2] else b""))
        if parent is not None:
            body += _uint(5, parent)
        if counter:
            body += _bytes(8, b"")
        if ordering is not None:
            body += _uint(11, ordering)
        if rank is not None:
            body += _sint(12, rank)
        self.descriptors.append(self.packet(_bytes(PK_TRACK_DESCRIPTOR, body)))
        return uuid

    def _event(self, ts, kind, track, name=None, category=None, args=None, flow=None, terminate=None, counter=None):
        body = _uint(9, kind) + _uint(11, track)
        if name is not None:
            body += _bytes(23, name)
        if category:
            body += _bytes(22, category)
        for key, value in (args or {}).items():
            body += _annotation(key, value)
        if counter is not None:
            body += _double(44, counter)
        for f in flow or ():
            body += _fixed64(47, f)
        for f in terminate or ():
            body += _fixed64(48, f)
        return _bytes(PK_TRACK_EVENT, body)

    def slice(self, track, start_ns, end_ns, name, category=None, args=None, flow=None, terminate=None, depth=0):
        end_ns = max(end_ns, start_ns + 1)
        self.events.append((start_ns, 1, depth, self._event(start_ns, TE_BEGIN, track, name, category, args, flow, terminate)))
        self.events.append((end_ns, 0, -depth, self._event(end_ns, TE_END, track)))

    def instant(self, track, ts, name, category=None, args=None):
        self.events.append((ts, 2, 0, self._event(ts, TE_INSTANT, track, name, category, args)))

    def counter(self, track, ts, value):
        self.events.append((ts, 2, 0, self._event(ts, TE_COUNTER, track, counter=value)))

    def nest(self, track, slices):
        """Writes [(start_ns, end_ns, name, category, args, flow, terminate)] on one track, clipping a slice that
        overlaps the end of its enclosing one so the track stays a proper stack. Returns how many were clipped."""
        clipped = 0
        stack = []
        for start, end, name, category, args, flow, terminate in sorted(slices, key=lambda s: (s[0], -(s[1] - s[0]))):
            while stack and stack[-1] <= start:
                stack.pop()
            if stack and end > stack[-1]:
                end = stack[-1]
                clipped += 1
            if end <= start:
                end = start + 1
                if stack and end > stack[-1]:
                    continue
            self.slice(track, start, end, name, category, args, flow, terminate, depth=len(stack))
            stack.append(end)
        return clipped

    def samples(self, seq, pid, tid, interned, samples):
        """Callstack samples of one thread: `interned` is (function_names, frames, callstacks, mappings) as lists of
        encoded messages; `samples` is [(ts_ns, callstack_iid)] in time order."""
        if not samples:
            return
        function_names, frames, callstacks, mappings, paths = interned
        reference_us = samples[0][0] // 1000
        descriptor = _bytes(PK_THREAD_DESCRIPTOR, _uint(1, pid) + _uint(2, tid) + _uint(6, reference_us))
        intern = b"".join(_bytes(5, m) for m in function_names) + b"".join(_bytes(6, m) for m in frames)
        intern += b"".join(_bytes(7, m) for m in callstacks) + b"".join(_bytes(17, m) for m in paths)
        intern += b"".join(_bytes(19, m) for m in mappings)
        self.sample_packets.append(self.packet(descriptor + _bytes(PK_INTERNED, intern), seq=seq, flags=SEQ_CLEARED | SEQ_NEEDS))
        last = reference_us
        for chunk_start in range(0, len(samples), 2000):
            chunk = samples[chunk_start:chunk_start + 2000]
            deltas = []
            for ts, _ in chunk:
                us = ts // 1000
                deltas.append(us - last)
                last = us
            body = _repeated(1, (iid for _, iid in chunk)) + _repeated(2, deltas)
            self.sample_packets.append(self.packet(_bytes(PK_STREAMING_PROFILE, body), seq=seq, flags=SEQ_NEEDS))

    def flow_id(self):
        self.flows += 1
        return self.flows

    def bytes(self):
        self.events.sort(key=lambda e: (e[0], e[1], e[2]))
        out = bytearray()
        # Sample timestamps are on the monotonic clock and track events on boottime; one snapshot at the same
        # value declares them identical (both are the game's QPC clock here).
        clocks = b"".join(_bytes(1, _uint(1, clock) + _uint(2, BASE_NS)) for clock in (3, 6))
        out += _bytes(1, _bytes(6, clocks + _uint(2, 6)) + _uint(PK_SEQ, self.TRACK_SEQ))
        for packet in self.descriptors:
            out += packet
        for ts, _, _, body in self.events:
            out += self.packet(body, ts=ts)
        for packet in self.sample_packets:
            out += packet
        return bytes(out)


# ---- the export ----------------------------------------------------------------------------------

BASE_NS = 1_000_000_000  # keeps events recorded just before the profile's start at positive times


def _ns(ms):
    return BASE_NS + int(round(ms * 1e6))


def _thread_names(profile, decoded):
    names = {}
    for thread in decoded["threads"] if decoded else []:
        if thread["name"]:
            names[thread["tid"]] = thread["name"]
    for thread in profile.raw.get("threads", []):
        name = thread.get("name") or ""
        if thread.get("main"):
            name = name or "main"
        if thread.get("frameThread") and "render" not in name.lower():
            name = (name + " (frame)").strip()
        if name:
            names.setdefault(thread["tid"], name)
    return names


def _sample_interning(profile, stacks):
    """Interned function names, frames, callstacks and module mappings for the given stacks."""
    functions, frames, mappings = {}, [], {}
    function_names, callstacks, mapping_msgs, paths = [], [], [], []
    for stack in sorted(stacks):
        for function in profile.stack_functions(stack):
            if function in functions:
                continue
            label, module, _ = profile.functions[function]
            if module not in mappings:
                mappings[module] = len(mappings) + 1
                paths.append(_uint(1, mappings[module]) + _bytes(2, module))
                mapping_msgs.append(_uint(1, mappings[module]) + _repeated(7, [mappings[module]]))
            iid = functions[function] = len(functions) + 1
            function_names.append(_uint(1, iid) + _bytes(2, label))
            rva = profile.function_meta[function].get("rva")
            frames.append(_uint(1, iid) + _uint(2, iid) + _uint(3, mappings[module]) + _uint(4, rva if isinstance(rva, int) else iid))
    for stack in sorted(stacks):
        chain = [functions[f] for f in reversed(profile.stack_functions(stack))]
        callstacks.append(_uint(1, stack + 1) + _repeated(2, chain))
    return function_names, frames, callstacks, mapping_msgs, paths


def build_perfetto(profile, recorder_marks=None):
    """The profile as Perfetto protobuf bytes plus what was written (track and event counts)."""
    raw = profile.raw
    decoded = decode(raw)
    pid = int(raw.get("pid") or 1000)
    writer = PerfettoWriter(pid)
    stats = defaultdict(int)
    process = writer.track(_uuid("process", pid), process=(pid, "SkyrimSE"))
    names = _thread_names(profile, decoded)
    thread_tracks = {}

    def thread_track(tid):
        if tid not in thread_tracks:
            thread_tracks[tid] = writer.track(_uuid("thread", tid), thread=(pid, tid, names.get(tid, "")), parent=process)
            stats["threads"] += 1
        return thread_tracks[tid]

    def child_track(tid, label, rank):
        return writer.track(_uuid("thread-child", tid, label), name=label, parent=thread_track(tid), rank=rank)

    period = 1000.0 / raw.get("hz", 1000.0)

    # CPU samples: every on-CPU tick; a waiting run gives one sample so its wait stack is still visible.
    for index, thread in enumerate(raw.get("threads", [])):
        tid = thread["tid"]
        thread_track(tid)
        runs = thread.get("runs", [])
        samples, used = [], set()
        states = []
        for at, stack, count, flags in runs:
            waiting = profile.is_wait(stack, flags)
            used.add(stack)
            ticks = 1 if waiting else count
            samples.extend((_ns(at + i * period), stack + 1) for i in range(ticks))
            state = "waiting" if waiting else "running"
            end = at + count * period
            if states and states[-1][2] == state and abs(states[-1][1] - at) < period * 1.5:
                states[-1][1] = end
            else:
                states.append([at, end, state])
        if samples:
            writer.samples(100 + index, pid, tid, _sample_interning(profile, used), samples)
            stats["samples"] += len(samples)
        if states:
            track = child_track(tid, "state (sampled)", 30)
            for start, end, state in states:
                writer.slice(track, _ns(start), _ns(end), state, "state")
            stats["stateSlices"] += len(states)

    # Trace events per thread: engine ranges, waits and reads on the thread; D3D11 calls and passes below it.
    gpu, _ = gpu_frames(raw)
    gpu_by_frame = {f["frame"]: f for f in gpu}
    flows = {}
    if decoded:
        for thread in decoded["threads"]:
            tid = thread["tid"]
            per_track = defaultdict(list)
            frames_of = pass_frames(thread["events"], decoded["names"])
            for index, (start, ms, name_id, flags, a, b) in enumerate(thread["events"]):
                name, category = decoded["names"][name_id] if name_id < len(decoded["names"]) else (f"#{name_id}", "")
                args = {k: v for k, v in event_args(category, name, flags, a, b).items() if v is not None}
                flow = None
                if category == "pass":
                    frame = frames_of.get(index)
                    if frame is not None:
                        args["frame"] = frame
                        gpu_frame = gpu_by_frame.get(frame)
                        # Pass indexes line up only when no GPU stamp was dropped, so the labels must agree too; a GPU
                        # pass placed before its submit means the clock alignment is off and the arrow would mislead.
                        gpu_pass = gpu_frame["passes"][flags] if gpu_frame and flags < len(gpu_frame["passes"]) else None
                        if gpu_pass and name == f"pass {gpu_pass[0]}" and gpu_pass[2] >= start:
                            flow = [writer.flow_id()]
                            flows[(frame, flags)] = flow[0]
                    lane = "D3D11 passes"
                elif category == "d3d11":
                    lane = "D3D11 API"
                else:
                    lane = None
                per_track[lane].append((_ns(start), _ns(start + ms), name, category, args, flow, None))
                stats[f"events:{category}"] += 1
            for lane, slices in per_track.items():
                if lane is None:
                    track = thread_track(tid)
                else:
                    track = child_track(tid, lane, 10 if lane == "D3D11 passes" else 20)
                stats["clipped"] += writer.nest(track, slices)

    # GPU: frames and passes on the CPU clock, flows from the CPU pass that submitted them, board counters.
    if gpu or board_series(raw):
        gpu_root = writer.track(_uuid("gpu"), name="GPU", rank=-10)
        if gpu:
            frame_track = writer.track(_uuid("gpu-frames"), name="GPU frames", parent=gpu_root, rank=0)
            pass_track = writer.track(_uuid("gpu-passes"), name="GPU passes", parent=gpu_root, rank=1)
            for f in gpu:
                writer.slice(frame_track, _ns(f["gpuStart"]), _ns(f["gpuEnd"]), f"frame {f['frame']}", "gpu",
                             {"gpu_ms": f["gpuMs"], "dropped_passes": f.get("droppedPasses", 0)})
                for k, (label, kind, start, end) in enumerate(f["passes"]):
                    flow = flows.get((f["frame"], k))
                    writer.slice(pass_track, _ns(start), _ns(end), label, "gpu",
                                 {"kind": kind, "pass_index": k, "frame": f["frame"], "ms": round(end - start, 3)},
                                 terminate=[flow] if flow else None)
                    stats["gpuPasses"] += 1
                    stats["flows"] += bool(flow)
        board_names = sorted({name for _, name, _ in board_series(raw)})
        board_tracks = {name: writer.track(_uuid("gpu-counter", name), name=name, parent=gpu_root, counter=True, rank=10 + i)
                        for i, name in enumerate(board_names)}
        for at, name, value in board_series(raw):
            writer.counter(board_tracks[name], _ns(at), value)
            stats["counterValues"] += 1

    # CPU frames as the game presented them.
    frame_marks = raw.get("frameMarks", [])
    if frame_marks:
        track = writer.track(_uuid("frames"), name="Frames (CPU present to present)", parent=process, rank=-5)
        for start, end, frame in frame_marks:
            writer.slice(track, _ns(start), _ns(end), f"frame {frame}", "frame", {"ms": round(end - start, 3)})

    # Counters: CPU, memory, page faults and I/O from the plugin's 250 ms series.
    board = {name for _, name, _ in board_series(raw)}
    series = [(at, name, value) for at, name, value in counter_series(raw) if name not in board]
    if series:
        group = writer.track(_uuid("counters"), name="Counters", rank=-8)
        tracks = {}
        for at, name, value in series:
            if name not in tracks:
                tracks[name] = writer.track(_uuid("counter", name), name=name, parent=group, counter=True, rank=len(tracks))
            writer.counter(tracks[name], _ns(at), value)
            stats["counterValues"] += 1

    # Markers: Profile.mark ranges, loading screens and menus, hitches, agent tool calls and narration.
    marks = [list(m) for m in raw.get("marks", [])] + [list(m) for m in recorder_marks or []]
    hitches = [h for h in raw.get("hitches", []) if h.get("startMs") is not None]
    if marks or hitches:
        group = writer.track(_uuid("markers"), name="Markers / Agent", rank=-9)
        lanes = {}
        for start, end, kind, text in marks:
            lane = "agent" if kind in ("tool", "narration", "agent") else kind
            if lane not in lanes:
                lanes[lane] = writer.track(_uuid("marker-lane", lane), name=lane, parent=group, rank=len(lanes))
            if end is None or end <= start:
                writer.instant(lanes[lane], _ns(start), text, kind)
            else:
                writer.slice(lanes[lane], _ns(start), _ns(end), text, kind)
            stats["markers"] += 1
        if hitches:
            track = writer.track(_uuid("marker-lane", "hitch"), name="hitches", parent=group, rank=100)
            for hitch in hitches:
                writer.slice(track, _ns(hitch["startMs"]), _ns(hitch["startMs"] + hitch.get("ms", 0)),
                             f"hitch frame {hitch.get('frame')}", "hitch", {"ms": hitch.get("ms")})
                stats["markers"] += 1

    stats["tracks"] = len(writer.descriptors)
    return writer.bytes(), dict(stats)


def write_perfetto(profile, path, recorder_marks=None):
    data, stats = build_perfetto(profile, recorder_marks)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stats["bytes"] = len(data)
    return stats


def write_chrome_json(profile, path, recorder_marks=None):
    """The same content as Chrome trace-event JSON (no callstack samples), for tools without protobuf import."""
    raw = profile.raw
    decoded = decode(raw)
    pid = int(raw.get("pid") or 1000)
    names = _thread_names(profile, decoded)
    events = [{"ph": "M", "pid": pid, "name": "process_name", "args": {"name": "SkyrimSE"}}]
    for tid, name in names.items():
        events.append({"ph": "M", "pid": pid, "tid": tid, "name": "thread_name", "args": {"name": name}})
    if decoded:
        for thread in decoded["threads"]:
            for start, ms, name_id, flags, a, b in thread["events"]:
                name, category = decoded["names"][name_id] if name_id < len(decoded["names"]) else (f"#{name_id}", "")
                events.append({"ph": "X", "pid": pid, "tid": thread["tid"], "ts": round(start * 1000.0, 3), "dur": round(ms * 1000.0, 3),
                               "name": name, "cat": category, "args": event_args(category, name, flags, a, b)})
    gpu, _ = gpu_frames(raw)
    for f in gpu:
        for k, (label, kind, start, end) in enumerate(f["passes"]):
            events.append({"ph": "X", "pid": pid, "tid": "GPU", "ts": round(start * 1000.0, 3), "dur": round((end - start) * 1000.0, 3),
                           "name": label, "cat": "gpu", "args": {"frame": f["frame"], "kind": kind, "pass_index": k}})
    for at, name, value in counter_series(raw):
        events.append({"ph": "C", "pid": pid, "ts": round(at * 1000.0, 3), "name": name, "args": {"value": value}})
    for start, end, kind, text in [list(m) for m in raw.get("marks", [])] + [list(m) for m in recorder_marks or []]:
        events.append({"ph": "X", "pid": pid, "tid": "Markers", "ts": round(start * 1000.0, 3),
                       "dur": round(max(0.0, (end or start) - start) * 1000.0, 3), "name": text, "cat": kind})
    Path(path).write_text(json.dumps({"traceEvents": events, "displayTimeUnit": "ms"}), encoding="utf-8")
    return {"events": len(events)}


# ---- summary and SQLite rows ---------------------------------------------------------------------


def sqlite_rows(raw):
    """TraceEvents rows: (threadId, start, end, ms, name, category, passIndex, a, b)."""
    decoded = decode(raw)
    if not decoded:
        return []
    rows = []
    for thread in decoded["threads"]:
        for start, ms, name_id, flags, a, b in thread["events"]:
            name, category = decoded["names"][name_id] if name_id < len(decoded["names"]) else (f"#{name_id}", "")
            rows.append((thread["tid"], round(start, 4), round(start + ms, 4), round(ms, 4), name, category, flags,
                         a if a < (1 << 63) else a - (1 << 64), b if b < (1 << 63) else b - (1 << 64)))
    return rows


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1)))], 3)


def summary(raw, top=10):
    """Per-frame engine-phase breakdown, top D3D11 costs, waits and reads of a traced profile."""
    decoded = decode(raw)
    if not decoded:
        return {"traced": False, "reason": "profile was recorded without trace"}
    frames = sorted(raw.get("frameMarks", []))
    starts = [f[0] for f in frames]
    def frame_of(ms):
        index = bisect.bisect_right(starts, ms) - 1
        return frames[index][2] if index >= 0 and ms < frames[index][1] else None

    phase = defaultdict(lambda: defaultdict(float))
    phase_calls = defaultdict(int)
    api_ms = defaultdict(float)
    api_calls = defaultdict(int)
    draws_per_frame = defaultdict(int)
    pass_api = defaultdict(lambda: [0.0, 0, 0])
    waits = defaultdict(lambda: [0, 0.0, 0.0])
    reads = defaultdict(lambda: [0, 0, 0.0])
    events = dropped = short = 0
    for thread in decoded["threads"]:
        dropped += thread["dropped"]
        short += thread["shortWaits"]
        for start, ms, name_id, flags, a, b in thread["events"]:
            events += 1
            name, category = decoded["names"][name_id] if name_id < len(decoded["names"]) else (f"#{name_id}", "")
            frame = frame_of(start)
            if category in ("wait",):
                entry = waits[(thread["name"] or str(thread["tid"]), name)]
                entry[0] += 1
                entry[1] += ms
                entry[2] = max(entry[2], ms)
            elif category == "io":
                entry = reads[name]
                entry[0] += 1
                entry[1] += a
                entry[2] += ms
            elif category == "pass":
                args = event_args(category, name, flags, a, b)
                if frame is not None:
                    draws_per_frame[frame] += args["draws"]
                entry = pass_api[name]
                entry[0] += args["api_us"] / 1000.0
                entry[1] += args["draws"]
                entry[2] += 1
            elif category == "d3d11":
                api_ms[name] += ms
                api_calls[name] += 1
                if name.startswith("Draw") and frame is not None and decoded["settings"].get("api") == "full":
                    draws_per_frame[frame] += 1
            else:
                phase_calls[name] += 1
                if frame is not None:
                    phase[frame][name] += ms
    counted = max(1, len(frames))
    labels = sorted({n for f in phase.values() for n in f}, key=lambda n: -sum(f.get(n, 0.0) for f in phase.values()))
    phases = []
    for label in labels[:top * 2]:
        per = [phase[f[2]].get(label, 0.0) for f in frames]
        phases.append({"phase": label, "calls": phase_calls[label], "msPerFrame": round(sum(per) / counted, 3),
                       "p95Ms": _pct(per, 0.95), "maxMs": round(max(per), 3) if per else None})
    longest = sorted(frames, key=lambda f: f[0] - f[1])[:5]
    worst = [{"frame": f[2], "ms": round(f[1] - f[0], 3),
              "phases": {k: round(v, 3) for k, v in sorted(phase[f[2]].items(), key=lambda kv: -kv[1])[:6]}} for f in longest]
    draws = list(draws_per_frame.values())
    return {
        "traced": True, "settings": decoded["settings"], "events": events, "droppedEvents": dropped, "shortWaitsCounted": short,
        "frames": len(frames), "phases": phases, "longestFrames": worst,
        "drawsPerFrame": {"p50": _pct(draws, 0.5), "p95": _pct(draws, 0.95), "max": max(draws) if draws else None},
        "apiPasses": [{"pass": k, "apiMsPerFrame": round(v[0] / counted, 3), "drawsPerFrame": round(v[1] / counted, 1)}
                      for k, v in sorted(pass_api.items(), key=lambda kv: -kv[1][0])[:top]],
        "apiCalls": [{"call": k, "calls": api_calls[k], "ms": round(v, 3)} for k, v in sorted(api_ms.items(), key=lambda kv: -kv[1])[:top]],
        "waits": [{"thread": t, "wait": n, "count": c, "ms": round(total, 3), "maxMs": round(mx, 3)}
                  for (t, n), (c, total, mx) in sorted(waits.items(), key=lambda kv: -kv[1][1])[:top]],
        "reads": [{"read": k, "count": c, "mb": round(b / 1048576.0, 3), "ms": round(ms, 3)} for k, (c, b, ms) in sorted(reads.items())],
        "hooks": [{k: h.get(k) for k in ("label", "calls", "filtered", "error") if h.get(k) is not None} for h in decoded["hooks"]],
        "api": {k: decoded["api"].get(k) for k in ("installed", "reason", "calls", "frames")},
    }


# ---- hook lists ----------------------------------------------------------------------------------


def local_hooks(paths, symbols=None):
    """Hooks from local JSON lists ([{label, name|rva|id, category, minUs}]). A `name` is looked up in the local
    symbol tables (exact function name); unresolved entries are returned under "unresolved"."""
    hooks, unresolved = [], []
    by_name = {}
    if symbols is not None:
        for rva, name in symbols.names.items():
            by_name.setdefault(name, rva)
    for path in paths:
        path = Path(path).expanduser()
        if not path.is_file():
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries.get("hooks", entries) if isinstance(entries, dict) else entries:
            item = {k: entry[k] for k in ("label", "category", "id", "rva", "minUs") if k in entry}
            if "id" not in item and "rva" not in item:
                rva = by_name.get(entry.get("name"))
                if rva is None:
                    unresolved.append(entry.get("name") or entry.get("label"))
                    continue
                item["rva"] = rva
            item.setdefault("label", entry.get("name") or str(item.get("id") or item.get("rva")))
            item.setdefault("category", "engine")
            hooks.append(item)
    return hooks, unresolved


def trace_config(api="batched", os_waits=True, wait_min_us=20.0, hooks=None, max_events=2_000_000):
    """The `trace` object Profile.start takes. Hooks from later lists replace earlier ones with the same label."""
    merged = {}
    for hook in [*DEFAULT_HOOKS, *(hooks or [])]:
        merged[hook["label"]] = hook
    return {"enabled": True, "api": api, "os": os_waits, "waitMinUs": wait_min_us, "maxEvents": max_events,
            "hooks": list(merged.values())}


def serve_and_open(path, port=9001):
    """Serves one trace file on 127.0.0.1 and opens ui.perfetto.dev on it; blocks until Ctrl-C. The file stays on
    this machine: the UI fetches it from localhost into the browser and nothing is uploaded."""
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import webbrowser

    path = Path(path).resolve()

    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "https://ui.perfetto.dev")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def do_GET(self):  # noqa: N802 - http.server naming
            if self.path.lstrip("/") != path.name:
                self.send_error(404)
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(path.parent)))
    url = f"https://ui.perfetto.dev/#!/?url=http://127.0.0.1:{port}/{path.name}"
    print(url)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
