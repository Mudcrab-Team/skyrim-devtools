"""Per-session timeline of everything the agent did, for the showcase video composer.

One JSON object per line, all times in UTC epoch milliseconds so they line up with the footage start
that `capture_start` records:
  {"t":"session","ms":..}                                  first line
  {"t":"tool","name":..,"args":{..},"startMs":..,"endMs":..,"ok":bool,"summary":"..","error":".."}
  {"t":"narrate","ms":..,"text":".."}                      caption the agent wants on screen
  {"t":"status","ms":..,"cell":..,"pos":[x,y,z],"health":[cur,max],"quest":{..},"nav":".."}
  {"t":"event","ms":..,"type":"moveStuck"|"moveUnstuck","data":{..}}   movement trouble reported by the plugin
  {"t":"capture","ms":..,"action":"start"|"stop","startEpochMs":..,"file":".."}
  {"t":"alert","ms":..,"action":"raised"|"cleared","kind":..,"severity":..,"hint":..,"detail":..}
"""

import asyncio
import contextlib
import functools
import inspect
import json
import os
from pathlib import Path
import time

SUMMARY_LIMIT = 220
ARG_LIMIT = 120
# tools whose own arguments or results carry nothing worth reading on screen
QUIET_TOOLS = {"narrate"}
# the alerts tool shows the alerts itself
NO_ALERT_FIELD = {"agent_alerts"}


def now_ms():
    return int(time.time() * 1000)


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact_args(args, defaults=None):
    """Arguments worth showing: unset ones and those left at their default are dropped."""
    out = {}
    for key, value in args.items():
        if value is None or (defaults and key in defaults and defaults[key] == value):
            continue
        out[key] = value if isinstance(value, (bool, int, float)) else clip(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value, ARG_LIMIT)
    return out


def summarize_result(result):
    """A short human-readable line for a tool result (text, image or structured content)."""
    if isinstance(result, (list, tuple)):
        parts = [summarize_result(item) for item in result]
        return clip(" ".join(p for p in parts if p), SUMMARY_LIMIT)
    if isinstance(result, str):
        return clip(result, SUMMARY_LIMIT)
    kind = type(result).__name__
    if kind == "Image":
        return "(image)"
    text = getattr(result, "text", None)
    return clip(text, SUMMARY_LIMIT) if isinstance(text, str) else clip(result, SUMMARY_LIMIT)


class Recorder:
    """Appends timeline lines to a file; safe to call from tool wrappers and the status sampler."""

    def __init__(self, directory=None, stamp=None):
        base = Path(directory or os.environ.get("SKYRIM_USE_TIMELINE_DIR") or "~/.skyrim-use/timelines").expanduser()
        base.mkdir(parents=True, exist_ok=True)
        self.directory = base
        self.stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.path = base / f"{self.stamp}.jsonl"
        self.capture = None
        self.track_quest = None
        self.status_task = None
        self.last_status = None
        self.write({"t": "session", "ms": now_ms()})

    def write(self, record):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    MOVE_EVENTS = ("moveStuck", "moveUnstuck")

    def attach(self, manager):
        """Write the plugin's movement-trouble events into the timeline as they arrive."""
        def on_event(event):
            if event.get("type") in self.MOVE_EVENTS:
                self.write({"t": "event", "ms": now_ms(), "type": event["type"], "data": event.get("data")})

        manager.listeners.append(on_event)

    def narrate(self, text):
        self.write({"t": "narrate", "ms": now_ms(), "text": text.strip()})

    def instrument(self, mcp, board=None):
        """Make every tool registered on `mcp` afterwards log itself and, with a board, carry its active alerts."""
        register = mcp.tool

        def tool(*dargs, **dkwargs):
            decorate = register(*dargs, **dkwargs)

            def apply(function):
                name = dkwargs.get("name") or function.__name__
                defaults = {k: p.default for k, p in inspect.signature(function).parameters.items()
                            if p.default is not inspect.Parameter.empty}
                if name in QUIET_TOOLS:
                    return decorate(function)

                @functools.wraps(function)
                async def logged(*args, **kwargs):
                    start = now_ms()
                    if board is not None:
                        board.call_started(name)
                    record = {"t": "tool", "name": name, "args": compact_args(kwargs, defaults), "startMs": start}
                    try:
                        result = await function(*args, **kwargs)
                    except BaseException as error:
                        record.update(endMs=now_ms(), ok=False, error=clip(f"{type(error).__name__}: {error}", SUMMARY_LIMIT))
                        self.write(record)
                        if board is not None and isinstance(error, Exception):
                            # a call that fails the same way again and again is a loop too
                            board.observe_call(name, kwargs, f"error: {error}")
                        raise
                    record.update(endMs=now_ms(), ok=True, summary=summarize_result(result))
                    self.write(record)
                    if board is not None:
                        board.observe_call(name, kwargs, result)
                        board.expire()
                        if name not in NO_ALERT_FIELD:
                            result = board.attach(result)
                    return result

                return decorate(logged)

            return apply

        mcp.tool = tool

    def start_sampler(self, manager, interval=4.0):
        if self.status_task is None and interval > 0:
            self.status_task = asyncio.create_task(self._sample_loop(manager, interval))

    async def stop_sampler(self):
        task, self.status_task = self.status_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def sample_now(self, manager):
        """Write a status line immediately; used when a recording or tracked quest starts so the panel is never blank."""
        self.last_status = None
        with contextlib.suppress(Exception):
            sample = await self.sample(manager)
            if sample:
                self.write({"t": "status", "ms": now_ms(), **sample})

    async def _sample_loop(self, manager, interval):
        while True:
            await asyncio.sleep(interval)
            if not manager.alive or manager.lock.locked():
                continue
            with contextlib.suppress(Exception):
                sample = await self.sample(manager)
                if sample:
                    self.write({"t": "status", "ms": now_ms(), **sample})

    async def sample(self, manager):
        """Cheap state probe; any field the plugin build or the current game state cannot answer is omitted."""
        sample = {}
        if manager.has_method("Player.state"):
            with contextlib.suppress(Exception):
                state = await manager.call("Player.state", timeout=5)
                cell = state.get("cell")
                # an exterior cell has no name; the location (Riverwood) reads better than its editor ID
                sample["cell"] = (state.get("location") or cell.get("name") or cell.get("editorID")
                                  if isinstance(cell, dict) else cell)
                pos = state.get("pos")
                if isinstance(pos, dict):
                    sample["pos"] = [round(pos.get(axis, 0)) for axis in "xyz"]
                elif isinstance(pos, list) and len(pos) == 3:
                    sample["pos"] = [round(v) for v in pos]
                health = state.get("health")
                if isinstance(health, dict):
                    sample["health"] = [health.get("cur"), health.get("max")]
                # a flag that is off is written as false: an absent key would leave the panel on the last true value
                for flag in ("inCombat", "dead", "sneaking"):
                    sample[flag] = bool(state.get(flag))
        quest = None
        if self.track_quest and manager.has_method("Quest.get"):
            with contextlib.suppress(Exception):
                quest = await manager.call("Quest.get", timeout=5, editorID=self.track_quest)
        elif manager.has_method("Quest.active"):
            with contextlib.suppress(Exception):
                listed = await manager.call("Quest.active", timeout=5, limit=1)
                quest = (listed.get("quests") or [None])[0]
        if quest:
            shown = [o for o in quest.get("objectives", []) if o.get("state") in ("displayed", "completedDisplayed")]
            sample["quest"] = {"name": quest.get("name") or quest.get("editorID"), "stage": quest.get("stage"),
                               "objective": shown[-1]["text"] if shown else None}
        if manager.has_method("Nav.status"):
            with contextlib.suppress(Exception):
                nav = await manager.call("Nav.status", timeout=5)
                sample["nav"] = clip(json.dumps(nav, ensure_ascii=False), 120)
                sample["walking"] = bool(isinstance(nav, dict) and (nav.get("active") or nav.get("moving")
                                                                    or nav.get("state") in ("walking", "moving", "running")))
        if sample == self.last_status:
            return None
        self.last_status = dict(sample)
        return sample
