"""One long-lived game session shared by every MCP tool call.

The manager owns the Windows host session, the SSH tunnel, the WebSocket and a background keepalive.
When the link drops it asks the host whether the game is still alive: a live game is reconnected, a
dead one is reported with the host's phase and reason so the caller can decide to start again.
"""

import asyncio
from collections import deque
import contextlib
from pathlib import Path
import re
import subprocess
import time

from ..client import MAX_MESSAGE, TIMEOUT, ConnectionLost, RpcError, Session, observe
from ..smoke import ACTIVE, WindowsHost
from .health import HealthWatch
from .options import split_options

KEEPALIVE_SECONDS = 15
HEALTH_SAMPLE_SECONDS = 60
START_RETRIES = 2
VANITY_OFF_SECONDS = 86400.0
EVENT_BUFFER = 5000
# console lines are already returned by the console tool, so subscribing would only add noise
QUIET_EVENTS = {"console"}
# delivered to listeners only: alerts reach the agent through the alerts field, not the event buffer
ALERT_EVENTS = {"alertRaised", "alertCleared"}
NOT_READY = -32004
KEEP_OPEN = {"HUD Menu", "Cursor Menu", "Main Menu", "Fader Menu", "LoadWaitSpinner", "Mist Menu"}
HINTS = {
    -32004: "the game is not ready yet (still starting, or no save is loaded); retry shortly",
    -32005: "the world was reloaded since you last looked; take a fresh snapshot",
    -32006: "the plugin's input hook is not available",
    -32007: "the console is not available yet",
    -32008: "the game did not answer in time (it may be loading or stalled)",
    -32010: "not found (unknown menu name or save name)",
}


def curated_methods():
    """Native methods the curated MCP tools call, read from the tool sources so the list cannot go stale."""
    names = set()
    for source in Path(__file__).parent.glob("*.py"):
        names.update(re.findall(r'\bnative\(\s*"([A-Za-z]+\.[A-Za-z]+)"', source.read_text(encoding="utf-8")))
    return sorted(names)


def plugin_report(info, methods):
    """Identify the plugin build and say plainly when it cannot serve the tools of this client."""
    build = info.get("pluginBuild")
    missing = [name for name in curated_methods() if name not in methods]
    report = {"buildId": build, "missingMethods": missing, "stale": bool(missing) or build is None}
    if build is None:
        report["warning"] = ("the plugin DLL on the host predates build identification: it is older than this client. "
                             "Redeploy it with tools/pc-build --deploy")
    if missing:
        report["warning"] = (f"the plugin DLL is older than this client: {len(missing)} curated method(s) are missing "
                             f"({', '.join(missing[:8])}{', ...' if len(missing) > 8 else ''}); tools that wrap them fail "
                             "and tools that exist in both builds may behave differently. Redeploy the plugin")
    return report


class GameError(RuntimeError):
    """A failure to report to the playtester as-is."""


def describe_rpc_error(error):
    text = f"{error} ({HINTS[error.code]})" if error.code in HINTS else str(error)
    if error.data:
        text += f" data={error.data}"
    return text


async def default_connect(port, token):
    from websockets.asyncio.client import connect

    return await connect(
        f"ws://127.0.0.1:{port}/rpc", additional_headers={"Authorization": f"Bearer {token}"},
        max_size=MAX_MESSAGE, open_timeout=10, ping_interval=None,
    )


def compact_event(event):
    return {"type": event.get("type"), "seq": event.get("seq"), "epoch": event.get("epoch"),
            "data": event.get("data")}


class GameManager:
    def __init__(self, config, host=None, connect=default_connect, keepalive=KEEPALIVE_SECONDS):
        self.config = config
        self.host = host or WindowsHost(config)
        self._connect = connect
        self.keepalive_seconds = keepalive
        self.event_listeners = []
        self.methods_listeners = []
        # Options outlive a game: they are applied again to every game this manager starts.
        self.option_state = dict(config.get("options") or {})
        split_options(self.option_state)
        self.option_state.setdefault("disableVanityCamera", not config.get("vanityCamera"))
        self._vanity_previous = None
        self._tasks = set()
        self.stall = None
        self.lock = asyncio.Lock()
        self.reset()

    def reset(self):
        self.phase = "idle"
        self.session = None
        self.session_id = None
        self.token = None
        self.port = None
        self.tunnel = None
        self.info = {}
        had_methods = bool(getattr(self, "methods", None))
        self.methods = {}
        if had_methods:
            self._methods_changed()
        self.stall = None
        self.mode = None
        self.crash = None
        self.keepalive_task = None
        self.events = deque(maxlen=EVENT_BUFFER)
        self.event_total = 0
        self.drain_cursor = 0
        self.subscribed = []
        self.stale_cleared = []
        self.health_watch = HealthWatch(self.config)
        self.health_sampled = 0.0

    @property
    def alive(self):
        return self.session is not None and self.phase == "running"

    def _on_event(self, event):
        compact = compact_event(event)
        for listener in self.event_listeners:
            try:
                listener(compact)
            except Exception:  # a broken listener must not cost the agent its event stream
                pass
        if compact["type"] == "methodsChanged":
            self._schedule(self.refresh_methods())
        elif compact["type"] == "mainThreadStalled":
            self.stall = dict(compact.get("data") or {})
        elif compact["type"] == "mainThreadResumed":
            self.stall = None
        if compact["type"] in ALERT_EVENTS:
            return
        self.events.append(compact)
        self.event_total += 1

    def _schedule(self, coroutine):
        try:
            task = asyncio.get_running_loop().create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _methods_changed(self):
        for listener in self.methods_listeners:
            try:
                listener()
            except Exception:  # a broken listener must not cost the agent its connection
                pass

    async def refresh_methods(self):
        """Re-read the method list after a plugin registered or removed methods at runtime."""
        if not self.alive:
            return
        with contextlib.suppress(ConnectionLost, OSError, RpcError, TimeoutError, asyncio.TimeoutError):
            await self._get_info()

    def note_warning(self, code, message):
        """Put a tool-side warning into the event buffer, where events_drain and the timeline can see it."""
        self._on_event({"type": "warning", "seq": None, "epoch": None, "data": {"code": code, "message": message}})

    def events_since(self, cursor):
        """Buffered events with absolute index >= cursor, plus how many were lost to the ring."""
        first = self.event_total - len(self.events)
        start = max(cursor, first)
        items = list(self.events)[start - first:]
        return items, max(0, first - cursor)

    def drain_events(self, types=None, limit=200):
        items, lost = self.events_since(self.drain_cursor)
        taken = []
        consumed = 0
        for item in items:
            if types and item["type"] not in types:
                consumed += 1
                continue
            if len(taken) >= limit:
                break
            taken.append(item)
            consumed += 1
        self.drain_cursor = max(self.drain_cursor, self.event_total - len(items)) + consumed
        return taken, lost, len(items) - consumed

    async def start(self, headless=True, save=None):
        """Launch the game. The engine occasionally dies within a second of launch (a null call on its audio
        thread, before any plugin code runs); such a start is repeated up to START_RETRIES times and reported
        under `startRetries`."""
        async with self.lock:
            if self.alive:
                return await self._status_locked(note="game already running; reusing the session")
            retries = []
            for attempt in range(START_RETRIES + 1):
                await self._teardown()
                self.reset()
                self.phase = "starting"
                self.mode = "headless" if headless else "windowed"
                try:
                    await self._launch(save)
                    break
                except BaseException as error:
                    early = await self._died_early(error)
                    if early and attempt < START_RETRIES:
                        retries.append(early)
                        await self._close_link()
                        continue
                    await self._abort(error)
                    if isinstance(error, (GameError, asyncio.CancelledError)):
                        raise
                    raise GameError(f"game_start failed: {type(error).__name__}: {error}") from error
            result = await self._status_locked(note="game started")
            if retries:
                result["startRetries"] = retries
            return result

    async def _died_early(self, error):
        """The crash summary when the game died during startup (so a fresh launch is worth trying), else None."""
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)) or not self.session_id:
            return None
        status = await self._host_status()
        if isinstance(error, (ConnectionLost, OSError)):
            # the socket drops a moment before the host notices the process is gone
            for _ in range(10):
                if status.get("phase") != "running":
                    break
                await asyncio.sleep(0.5)
                status = await self._host_status()
        if status.get("phase") != "crashed":
            return None
        self.crash = {"hostPhase": "crashed", "reason": status.get("reason"), "exitCode": status.get("exitCode")}
        await self._attach_crash_log(status)
        crash, self.crash = self.crash, None
        return crash

    async def _launch(self, save):
        bootstrap = await self.host.command("start", mode=self.mode)
        self.session_id, self.token, self.port = bootstrap["sessionId"], bootstrap["token"], bootstrap["port"]
        self.stale_cleared = list(bootstrap.get("staleCleared") or [])
        if not re.fullmatch("[a-f0-9]{32}", self.session_id) or not re.fullmatch("[a-f0-9]{64}", self.token):
            raise GameError("Invalid session bootstrap from the host")
        await self.host.wait_ready(self.session_id)
        # a link that drops while the plugin is still coming up is reopened; a dead game is left to start()
        for attempt in range(4):
            try:
                await self._attach()
                self.info = await observe(self._get_info, lambda s: s["dataLoaded"], "Game data", 120)
                await self._subscribe()
                break
            except (ConnectionLost, OSError):
                if attempt == 3 or (await self._host_status()).get("phase") != "running":
                    raise
                await self._close_link()
                await asyncio.sleep(1)
        await observe(self._snapshot_raw, lambda s: any(m["name"] == "Main Menu" for m in s["menus"]), "Main menu", 60)
        await self.apply_options()
        self.phase = "running"
        self.keepalive_task = asyncio.create_task(self._keepalive())
        if save:
            await self._load_save_locked(save)

    async def apply_options(self, changed=None):
        """Push the option state to the running game. The idle orbit camera starts after a couple of minutes without
        input, which is most of an agent's thinking time, and turns screenshots and recordings into a slow spin, so
        it is off unless disableVanityCamera is false (config vanityCamera=true keeps it too)."""
        keys = set(changed) if changed is not None else set(self.option_state)
        native, _ = split_options(self.option_state)
        if "disableVanityCamera" in keys and "Setting.set" in self.methods:
            await self._set_vanity(bool(self.option_state.get("disableVanityCamera")))
        native = {key: value for key, value in native.items() if key in keys}
        if native and "Options.set" in self.methods:
            return await self.session.call("Options.set", **native)
        return None

    async def _set_vanity(self, disable):
        with contextlib.suppress(RpcError, TimeoutError, asyncio.TimeoutError):
            if disable:
                reply = await self.session.call("Setting.set", name="fAutoVanityModeDelay:Camera", value=VANITY_OFF_SECONDS)
                if self._vanity_previous is None and isinstance(reply, dict) and reply.get("previous") != VANITY_OFF_SECONDS:
                    self._vanity_previous = reply.get("previous")
            elif self._vanity_previous is not None:
                await self.session.call("Setting.set", name="fAutoVanityModeDelay:Camera", value=self._vanity_previous)
                self._vanity_previous = None

    async def _get_info(self):
        info = await self.session.call("Session.getInfo")
        self.methods = {item["name"]: item for item in info.get("methods", [])}
        self._methods_changed()
        return info

    async def _snapshot_raw(self):
        # while the UI thread is still starting, a deadline miss means "not yet", not failure
        try:
            return await self.session.call("Events.snapshot")
        except RpcError as error:
            if error.code == TIMEOUT:
                raise RpcError(NOT_READY, "UI not answering yet") from error
            raise

    async def _attach(self):
        self.tunnel = await self.host.tunnel(self.port)
        socket = None
        # the forward needs a moment to accept connections
        for attempt in range(20):
            try:
                socket = await self._connect(self.host.local_port, self.token)
                break
            except OSError:
                if attempt == 19:
                    raise
                await asyncio.sleep(0.3)
        self.session = Session(socket, self.session_id, event_limit=1)
        self.session.on_event(self._on_event)

    async def _subscribe(self):
        listed = await self.session.call("Events.list")
        names = [item["type"] for item in listed]
        self.subscribed = [name for name in names if name not in QUIET_EVENTS] or ["menuOpenClose", "loadGame", "cellChange"]
        await self.session.call("Events.subscribe", types=self.subscribed)

    async def _load_save_locked(self, name):
        await self.session.call("Session.loadSave", timeout=120, name=name, wait=True)
        await observe(self._snapshot_raw, lambda s: s["playable"], "Save load", 90)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(self.keepalive_seconds)
            if self.session is None:
                return
            if time.monotonic() - self.health_sampled >= HEALTH_SAMPLE_SECONDS:
                # sampled in the background so the growth rate is known even if nobody asks for the status
                await self.sample_health()
            try:
                await self.session.call("Session.ping", timeout=20)
            except (ConnectionLost, OSError, TimeoutError, RpcError, asyncio.TimeoutError):
                async with self.lock:
                    if self.phase == "running":
                        await self._recover()
                return

    async def sample_health(self):
        """Read the game process health from the host; never raises (a failed read is reported, not fatal)."""
        self.health_sampled = time.monotonic()
        try:
            async with asyncio.timeout(20):
                raw = await self.host.command("health")
        except Exception as error:
            return {"error": f"health read failed: {type(error).__name__}"}
        health = self.health_watch.record(raw)
        return health if health is not None else {"running": False}

    async def _host_status(self):
        try:
            return await self.host.command("status", self.session_id)
        except Exception as error:
            return {"phase": "unknown", "reason": f"host status failed: {error}"}

    async def _recover(self):
        """Link lost: reconnect if the game is still alive, otherwise record how it ended."""
        status = await self._host_status()
        if status.get("phase") in ACTIVE:
            try:
                await self._close_link()
                await self._attach()
                await self._get_info()
                await self._subscribe()
                self.keepalive_task = asyncio.create_task(self._keepalive())
                return True
            except Exception as error:
                status = {**status, "reason": f"reconnect failed: {error}"}
        self.crash = {"hostPhase": status.get("phase"), "reason": status.get("reason"),
                      "exitCode": status.get("exitCode")}
        await self._attach_crash_log(status)
        self.phase = "crashed"
        await self._close_link()
        return False

    async def _attach_crash_log(self, status):
        """Add the crash logger's verdict (exception and top frames, by module and Address Library id) to self.crash."""
        if not self.session_id or not self.crash:
            return
        log = None
        try:
            evidence = {}
            if status.get("evidence"):
                evidence = await self.host.command("evidence", self.session_id)
            logs = evidence.get("crashLogs") or []
            if not logs and status.get("phase") in {"crashed", "exited", "forced"}:
                logs = (await self.host.command("crashes"))[:1]
            log = logs[0] if logs else None
        except Exception:
            return
        if log:
            exception = (log.get("exception") or "").split("\t")[0]
            self.crash["log"] = {"file": log.get("name"), "exception": exception, "topFrames": log.get("topFrames", [])}

    async def _close_link(self):
        session, self.session = self.session, None
        if session is not None:
            session._reader.cancel()
            with contextlib.suppress(Exception):
                await session.socket.close()
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None and tunnel.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                tunnel.terminate()
            with contextlib.suppress(Exception):
                await tunnel.wait()

    async def _abort(self, error):
        self.phase = "failed"
        self.crash = {"hostPhase": "start-failed", "reason": str(error), "exitCode": None}
        task = self.keepalive_task
        self.keepalive_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        await self._close_link()
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.host.stop(self.session_id)

    async def _teardown(self):
        task, self.keepalive_task = self.keepalive_task, None
        if task is not None:
            task.cancel()
        await self._close_link()

    async def _close_game_menus(self):
        """Shutting the game down with a gameplay menu open crashed it, so close those first (best effort)."""
        if self.session is None or self.session._closed is not None:
            return
        try:
            state = await self.session.call("Events.snapshot", timeout=8)
            for menu in state["menus"]:
                if menu["name"] not in KEEP_OPEN:
                    await self.session.call("UI.closeMenu", timeout=8, name=menu["name"], wait=True)
        except Exception:
            pass

    async def stop(self, grace=12):
        """Quit the game; the host asks it to close and kills it when it has not exited after `grace` seconds.
        The result's `path` says which way it ended: graceful, killed-after-Ns, closing-crashed or crashed."""
        async with self.lock:
            if self.session_id is None or self.phase in {"idle", "stopped"}:
                return {"phase": self.phase, "note": "no game is running"}
            task, self.keepalive_task = self.keepalive_task, None
            if task is not None:
                task.cancel()
            await self._close_game_menus()
            # stop through the host first: dropping the socket beforehand makes the game exit on its own,
            # which the host then records as an unrequested exit instead of a clean stop
            try:
                result = await self.host.stop(self.session_id, grace=grace)
            except Exception as error:
                result = {"phase": "unknown", "reason": str(error)}
            await self._close_link()
            self.phase = "stopped"
            clean = result.get("phase") == "stopped" and result.get("exitCode") == 0
            path = result.get("stopPath") or ("graceful" if clean else result.get("phase") or "unknown")
            outcome = {"phase": "stopped", "cleanExit": clean, "path": path,
                       "host": {k: result.get(k) for k in ("phase", "exitCode", "reason")}}
            if result.get("phase") == "crashed":
                self.crash = {"hostPhase": result.get("phase"), "reason": result.get("reason"),
                              "exitCode": result.get("exitCode")}
                await self._attach_crash_log(result)
                outcome["crash"] = self.crash
            return outcome

    async def call(self, method, timeout=None, **params):
        """Call a native method; transparently recovers a dropped link once."""
        if self.phase == "crashed":
            raise GameError(self._crash_text())
        if not self.alive:
            raise GameError(f"no game is running (phase: {self.phase}); call game_start first")
        for attempt in range(2):
            try:
                return await self.session.call(method, timeout=timeout, **params)
            except RpcError as error:
                raise GameError(describe_rpc_error(error)) from error
            except (ConnectionLost, OSError) as error:
                async with self.lock:
                    if self.phase == "running" and (self.session is None or self.session._closed is not None):
                        recovered = await self._recover()
                    else:
                        recovered = self.alive
                if not recovered:
                    raise GameError(self._crash_text()) from error
                if attempt:
                    raise GameError(f"connection lost during {method}") from error
        raise GameError(f"connection lost during {method}")

    def _crash_text(self):
        crash = self.crash or {}
        return (f"the game is gone (host phase {crash.get('hostPhase')}, reason {crash.get('reason')}, "
                f"exit code {crash.get('exitCode')}); call game_start to launch it again")

    def has_method(self, name):
        return name in self.methods

    async def _status_locked(self, note=None):
        result = {"phase": self.phase}
        if note:
            result["note"] = note
        if self.session_id:
            result.update(sessionId=self.session_id, mode=self.mode)
        if self.crash:
            result["crash"] = self.crash
        if self.alive:
            result["info"] = {key: self.info.get(key) for key in
                              ("protocol", "dataLoaded", "loadEpoch", "headless", "capabilities") if key in self.info}
            result["methodCount"] = len(self.methods)
            result["plugin"] = plugin_report(self.info, self.methods)
            if result["plugin"].get("warning"):
                result["warning"] = result["plugin"]["warning"]
            result["bufferedEvents"] = self.event_total - self.drain_cursor
            if self.stall:
                result["mainThreadStalled"] = self.stall
                note = f"the game's main thread has not run a task for {self.stall.get('stalledMs')} ms"
                result["warning"] = f"{result['warning']}; {note}" if result.get("warning") else note
        if self.stale_cleared:
            result["staleCleared"] = self.stale_cleared
        return result

    async def status(self):
        """Phase plus, for a live game, a fresh probe so a silent crash is noticed now."""
        if self.alive:
            try:
                self.info = await self._get_info()
            except (ConnectionLost, OSError, RpcError, TimeoutError):
                async with self.lock:
                    if self.phase == "running":
                        await self._recover()
        result = await self._status_locked()
        if self.phase == "running":
            # read even when the link is dead: a hung game still shows its thread count and "not responding"
            result["health"] = await self.sample_health()
            warnings = self.health_watch.warnings
            if warnings:
                # integration point for the alerts layer: raise each warning under its `code`
                result["healthWarnings"] = warnings
                text = "; ".join(w["message"] for w in warnings if w["severity"] != "info")
                if text:
                    result["warning"] = f"{result['warning']}; {text}" if result.get("warning") else text
        return result
