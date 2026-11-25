"""Detecting that something went wrong during a scenario.

Event streams, Papyrus error collection, main-thread liveness and the crash-versus-clean-stop
classification of a finished run. Everything here works on a connected Session or on the
host's state record; nothing touches the game directly.
"""

import asyncio
from dataclasses import dataclass, field
import re


SEVERITIES = ("info", "warning", "error", "fatal")
GAME_EVENTS = (
    "combat", "death", "hit", "containerChanged", "questStage",
    "activate", "equip", "levelUp", "spellCast", "magicEffectApply",
)


class EventWatch:
    """Async iterator over server notifications of the chosen types.

    Use as `async with session.watch("death", "combat") as events: async for event in events: ...`
    or poll with `await events.next(timeout=5)`. The watch has its own bounded queue, so it never
    competes with Session.events; events beyond the bound are counted in `dropped`.
    """

    def __init__(self, session, types, limit=10000):
        self.session = session
        self.types = tuple(types)
        self.dropped = 0
        self._limit = limit
        # Unbounded so the close marker always fits; the limit is applied when events are offered.
        self._queue = asyncio.Queue()

    async def __aenter__(self):
        self.session.on_event(self._offer)
        try:
            await self.session.subscribe(*self.types)
        except BaseException:
            self.session.off_event(self._offer)
            raise
        return self

    async def __aexit__(self, *exc):
        self.close()

    def close(self):
        self.session.off_event(self._offer)
        self._queue.put_nowait(None)

    def _offer(self, event):
        if event.get("type") not in self.types:
            return
        if self._queue.qsize() >= self._limit:
            self.dropped += 1
        else:
            self._queue.put_nowait(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)
            raise StopAsyncIteration
        return event

    async def next(self, predicate=None, timeout=10):
        """The next event for which predicate(event) is true; earlier ones are discarded."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self.__anext__()
                if predicate is None or predicate(event):
                    return event

    def drain(self):
        drained = []
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if event is not None:
                drained.append(event)
        return drained


class PapyrusErrors(AssertionError):
    def __init__(self, errors):
        self.errors = errors
        shown = "\n".join(f"  {line['text']}" for line in errors[:10])
        more = f"\n  ... and {len(errors) - 10} more" if len(errors) > 10 else ""
        super().__init__(f"{len(errors)} new Papyrus error(s):\n{shown}{more}")


class PapyrusWatch:
    """Collects Papyrus log lines written while the block runs.

        async with session.papyrus_watch() as papyrus:
            await session.console("setstage MQ101 10")
        papyrus.assert_clean()

    With strict=True leaving the block raises PapyrusErrors when new errors appeared. `ignore` is
    a list of regular expressions for lines that are known noise. `overflowed` reports that the
    plugin's bounded history dropped lines during the block. The history is read back from
    the plugin when the block ends, so a burst of lines cannot be lost to a slow event stream.
    """

    def __init__(self, session, strict=False, ignore=(), fail_on_warnings=False):
        self.session = session
        self.strict = strict
        self.fail_on_warnings = fail_on_warnings
        self._ignore = [re.compile(pattern) for pattern in ignore]
        self.mark = None
        self.lines = []
        self.overflowed = False
        self._dropped_at_start = 0

    async def __aenter__(self):
        start = await self.session.call("Papyrus.log", limit=0)
        self.mark = start["lastSeq"]
        self._dropped_at_start = start["dropped"]
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.collect()
        if self.strict and exc_type is None:
            self.assert_clean()

    async def collect(self):
        """Reads the lines logged since the block began; callable mid-scenario too."""
        result = await self.session.call("Papyrus.log", since=self.mark, level="warning", limit=5000)
        # The plugin keeps a bounded history; lines pushed out of it during the block are lost.
        self.overflowed = result["dropped"] > self._dropped_at_start
        self.lines = [line for line in result["lines"] if not any(p.search(line["text"]) for p in self._ignore)]
        return self.lines

    @property
    def errors(self):
        return [line for line in self.lines if line["severity"] in ("error", "fatal")]

    @property
    def warnings(self):
        return [line for line in self.lines if line["severity"] == "warning"]

    def assert_clean(self):
        """Raises PapyrusErrors when the block logged errors (or warnings with fail_on_warnings)."""
        bad = self.errors + (self.warnings if self.fail_on_warnings else [])
        if bad:
            raise PapyrusErrors(bad)


@dataclass
class Health:
    status: str  # ok, busy, hang, unresponsive, lost
    main_stalled_ms: int = 0
    ui_stalled_ms: int = 0
    uptime_ms: int = 0
    detail: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def alive(self):
        return self.status in ("ok", "busy")


async def heartbeat(session, hang_after=30.0, busy_after=5.0, timeout=5.0):
    """One liveness reading, answered by the plugin's connection thread.

    The plugin queues a probe onto the main and UI threads and reports how long an unanswered
    probe has been waiting, so this call returns at once even when the game thread is frozen:
      ok            both probes are answered promptly
      busy          a thread has not turned over for busy_after seconds (a long load, for example)
      hang          a thread has not turned over for hang_after seconds while the plugin still answers
      unresponsive  the plugin itself did not answer in `timeout` seconds
      lost          the connection is gone (the game exited or crashed)
    """
    from .client import ConnectionLost, RpcError

    try:
        raw = await session.call("Health.heartbeat", timeout=timeout)
    except ConnectionLost as error:
        return Health("lost", detail=str(error))
    except (TimeoutError, OSError) as error:
        return Health("unresponsive", detail=str(error) or "no answer from the plugin")
    except RpcError as error:
        return Health("unresponsive", detail=str(error))
    main = raw["main"]["stalledMs"]
    ui = raw["ui"]["stalledMs"]
    worst = max(main, ui)
    status = "hang" if worst >= hang_after * 1000 else "busy" if worst >= busy_after * 1000 else "ok"
    which = "main" if main >= ui else "UI"
    detail = f"{which} thread has not turned over for {worst / 1000:.1f}s" if status != "ok" else ""
    return Health(status, main, ui, raw["uptimeMs"], detail, raw)


class HangMonitor:
    """Polls heartbeat in the background and records the worst state seen.

        monitor = session.monitor(hang_after=30)
        ... run the scenario ...
        monitor.stop()
        assert not monitor.hung, monitor.detail

    `status` is the latest reading. `hung` latches once a hang, a lost connection, or an
    unresponsive plugin lasting hang_after seconds was observed. on_change(health) is called on
    every status transition.
    """

    def __init__(self, session, interval=2.0, hang_after=30.0, busy_after=5.0, on_change=None):
        self.session = session
        self.interval = interval
        self.hang_after = hang_after
        self.busy_after = busy_after
        self.on_change = on_change
        self.status = "unknown"
        self.detail = ""
        self.hung = False
        self.history = []
        self.hang_detected = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        clock = asyncio.get_running_loop().time
        silent_since = None
        while True:
            health = await heartbeat(self.session, self.hang_after, self.busy_after, timeout=self.interval * 2)
            now = clock()
            if health.status == "unresponsive":
                silent_since = silent_since or now
                if now - silent_since >= self.hang_after:
                    health = Health("hang", detail=f"the plugin has not answered for {now - silent_since:.0f}s")
            else:
                silent_since = None
            if health.status != self.status:
                self.history.append((self.status, health.status))
                self.status = health.status
                self.detail = health.detail
                if self.on_change:
                    self.on_change(health)
            if health.status in ("hang", "lost"):
                self.hung = True
                self.detail = health.detail
                self.hang_detected.set()
            if health.status == "lost":
                return
            await asyncio.sleep(self.interval)

    def stop(self):
        self._task.cancel()


@dataclass
class Outcome:
    """How a game run ended, from the host's state record and any collected evidence."""

    kind: str  # clean, crash, hang, forced, unexpected-exit, startup-failure, running
    phase: str
    reason: str = ""
    exit_code: int = None
    evidence: dict = None

    @property
    def clean(self):
        return self.kind == "clean"

    @property
    def summary(self):
        text = f"{self.kind} ({self.phase}"
        if self.reason:
            text += f", {self.reason}"
        if self.exit_code not in (None, 0):
            text += f", exit {self.exit_code & 0xFFFFFFFF:#010x}"
        text += ")"
        for event in (self.evidence or {}).get("crashEvents", []):
            if event.get("faultingModule"):
                text += f"; faulting module {event['faultingModule']}, exception {event.get('exceptionCode')}"
                break
        return text


def classify_outcome(state, evidence=None):
    """Turns a host state record ({phase, reason, exitCode, hung?}) into an Outcome."""
    phase = state.get("phase", "")
    reason = state.get("reason", "") or ""
    code = state.get("exitCode")
    if phase == "stopped" and code == 0:
        kind = "clean"
    elif phase == "crashed":
        kind = "crash"
    elif phase == "forced":
        kind = "hang" if state.get("hung") else "forced"
    elif phase == "exited":
        kind = "unexpected-exit"
    elif phase in ("failed", "cancelled"):
        kind = "startup-failure"
    elif phase in ("starting", "running"):
        kind = "hang" if state.get("hangSuspectedSince") else "running"
    else:
        kind = "forced"
    return Outcome(kind, phase, reason, code, evidence)
