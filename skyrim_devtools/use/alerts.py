"""Alerts: short, actionable notices that the agent is wasting time, attached to every tool response.

Most detectors run inside the game plugin (native/alerts.cpp) and arrive as `alertRaised` and
`alertCleared` events. This module adds the ones only the MCP side can see (the same call repeated with
nothing changing, a blank screenshot), maps the movement stuck detector's `moveStuck`/`moveUnstuck`
events onto alerts, keeps the history, writes timeline entries so recordings can mark them, and
decorates tool results with the active set.
"""

from collections import deque
import hashlib
import json
import time

DEFAULTS = {
    "actionLoopCount": 3,
    "actionLoopWindowS": 120,
    "blindMeanLuma": 12.0,
    "blindDominantShare": 0.92,
}

SEVERITIES = ("info", "warn", "error")
LOOP_QUIET_S = 30

# calls that wait or report by nature: repeating them says nothing about the agent being stuck
LOOP_EXEMPT = {"agent_alerts", "narrate", "game_status", "wait_for", "nav_wait", "dialogue_wait", "perf_check",
               "perf_frame_stats", "perf_hitches", "events_drain", "events_list", "rpc_methods", "game_start",
               "game_stop", "nav_status", "capture_status"}

# a new movement goal makes an earlier stuck report obsolete; a loaded save or a teleport does too
NEW_GOAL_TOOLS = {"nav_goto", "nav_cancel", "walk"}
WORLD_CHANGE_TOOLS = {"load_save", "reload", "center_on_cell"}

HINTS = {
    "actionLoop": "the same call keeps giving the same result: change the approach, read quest_targets, or look at a screenshot",
    "blindCamera": "the view is black or one colour: check for a menu or fade, look to level, or move the camera",
    "moveStuck": "movement is stuck: try another route, open the door by hand, jump, or nav_goto with another mode",
}


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def digest(value):
    """A stable fingerprint of a tool result, so 'nothing changed' can be told from 'something did'."""
    if isinstance(value, (list, tuple)):
        parts = [digest(item) for item in value]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()
    data = getattr(value, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return hashlib.sha1(bytes(data)).hexdigest()
    text = value if isinstance(value, str) else getattr(value, "text", None)
    if text is None:
        text = repr(value)
    try:
        # the result text of a screenshot or status call carries a changing frame number or timestamp
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = {k: v for k, v in parsed.items() if k not in VOLATILE}
        text = json.dumps(parsed, sort_keys=True)
    except (ValueError, TypeError):
        pass
    return hashlib.sha1(text.encode()).hexdigest()


VOLATILE = {"frame", "seq", "readbackMs", "encodeMs", "waitedMs", "elapsedMs", "epochMs", "ageS", "bytes", "alerts",
            "meanLuma", "dominantShare"}


class AlertBoard:
    def __init__(self, write=None, clock=time.time, config=None):
        self.write = write or (lambda record: None)
        self.clock = clock
        self.config = dict(DEFAULTS, **(config or {}))
        self.active = {}
        self.history = deque(maxlen=200)
        self.calls = deque(maxlen=64)
        self.loop_seen = 0.0
        self.disabled = set()
        self.snoozed = {}

    # ---- state ---------------------------------------------------------------------------------

    def now_ms(self):
        return int(self.clock() * 1000)

    def raise_(self, kind, severity, hint, detail="", since_ms=None, source="mcp"):
        if kind in self.disabled or self.snoozed.get(kind, 0) > self.clock():
            return False
        existing = self.active.get(kind)
        if existing:
            existing["detail"] = detail
            return False
        alert = {"kind": kind, "severity": severity, "hint": hint, "detail": detail,
                 "sinceMs": since_ms or self.now_ms(), "source": source}
        self.active[kind] = alert
        record = {"t": "alert", "ms": self.now_ms(), "action": "raised", "kind": kind, "severity": severity,
                  "hint": hint, "detail": detail, "source": source}
        self.history.append({k: v for k, v in record.items() if k != "t"})
        self.write(record)
        return True

    def clear(self, kind, why="condition ended"):
        alert = self.active.pop(kind, None)
        if alert is None:
            return False
        now = self.now_ms()
        record = {"t": "alert", "ms": now, "action": "cleared", "kind": kind, "durationMs": now - alert["sinceMs"],
                  "why": why, "source": alert.get("source", "mcp")}
        self.history.append({k: v for k, v in record.items() if k != "t"})
        self.write(record)
        return True

    def forget_native(self):
        """Drop plugin alerts when the game goes away, so a stale set is never shown for the next run."""
        for kind in [k for k, a in self.active.items() if a.get("source") in ("native", "move")]:
            self.clear(kind, "game session ended")

    def sync(self, state):
        """Align with a fresh Alerts.state reply, e.g. after events were lost or a reconnect."""
        native = {a["kind"]: a for a in state.get("active", [])}
        for kind, alert in native.items():
            if kind not in self.active:
                self.raise_(kind, alert["severity"], alert["hint"], alert.get("detail", ""), alert.get("sinceMs"), "native")
        for kind in [k for k, a in self.active.items() if a.get("source") == "native" and k not in native]:
            self.clear(kind, "no longer active in the game")

    def on_event(self, event):
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "alertRaised":
            self.raise_(data["kind"], data.get("severity", "warn"), data.get("hint", ""), data.get("detail", ""),
                        data.get("sinceMs"), "native")
        elif kind == "alertCleared":
            self.clear(data.get("kind", ""), data.get("why", "condition ended"))
        elif kind == "moveStuck":
            detail = data.get("reason") or data.get("detail") or data.get("kind") or ""
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail, separators=(",", ":"))[:160]
            self.raise_("moveStuck", "warn", HINTS["moveStuck"], str(detail), source="move")
        elif kind in ("moveUnstuck", "navArrived"):
            self.clear("moveStuck", "movement resumed" if kind == "moveUnstuck" else "arrived")
        elif kind == "loadGame":
            # the plugin's alerts that still hold are put back by the next sync with Alerts.state
            for active in list(self.active):
                self.clear(active, "a save was loaded")
        elif kind == "cellChange" and data.get("entered", True):
            for active in ("moveStuck", "blindCamera"):
                self.clear(active, "the player changed cell")

    def call_started(self, tool):
        """Called before a tool runs: a stuck report ends when the agent sets a new goal or leaves the place."""
        if tool in NEW_GOAL_TOOLS or tool in WORLD_CHANGE_TOOLS:
            self.clear("moveStuck", "a new movement goal was set" if tool in NEW_GOAL_TOOLS else "the world changed")

    # ---- MCP-side detectors --------------------------------------------------------------------

    def observe_call(self, tool, args, result):
        """Feed one finished tool call; raises actionLoop when the same call repeats with the same outcome."""
        if tool in LOOP_EXEMPT:
            return
        now = self.clock()
        key = (tool, json.dumps(args, sort_keys=True, default=str))
        outcome = digest(result)
        self.calls.append((now, key, outcome))
        window = self.config["actionLoopWindowS"]
        same = [c for c in self.calls if c[1] == key and now - c[0] <= window]
        # the outcome of the latest call is the reference: an earlier different outcome ends the run, because
        # then the call was making progress; calls to other tools in between do not reset the count
        unchanged = 0
        for when, call_key, call_outcome in reversed(same):
            if call_outcome != outcome:
                break
            unchanged += 1
        loop = self.active.get("actionLoop")
        if unchanged >= self.config["actionLoopCount"]:
            self.loop_seen = now
            detail = f"{tool} called {unchanged} times in {fmt_age(now - same[-unchanged][0])} with no change"
            if self.raise_("actionLoop", "warn", HINTS["actionLoop"], detail):
                self.active["actionLoop"]["key"] = key
        elif loop and loop.get("key") == key:
            self.clear("actionLoop", "the call's result changed")

    def expire(self):
        """The loop alert ends once the repeating call has not come back for a while."""
        if "actionLoop" in self.active and self.clock() - self.loop_seen > LOOP_QUIET_S:
            self.clear("actionLoop", "the repeated call stopped")

    def observe_screenshot(self, meta):
        """Screen.capture reports the mean luma and dominant-colour share of the frame it encoded."""
        if not isinstance(meta, dict) or "meanLuma" not in meta:
            return
        dark = meta["meanLuma"] <= self.config["blindMeanLuma"]
        flat = meta.get("dominantShare", 0) >= self.config["blindDominantShare"]
        if dark or flat:
            self.raise_("blindCamera", "info", HINTS["blindCamera"],
                        f"the last screenshot is {'very dark' if dark else 'mostly one colour'} "
                        f"(luma {meta['meanLuma']}, one colour {round(meta.get('dominantShare', 0) * 100)}%)", source="mcp-shot")
        elif self.active.get("blindCamera", {}).get("source") == "mcp-shot":
            self.clear("blindCamera", "the screenshot shows a scene again")

    # ---- output --------------------------------------------------------------------------------

    def compact(self):
        now = self.now_ms()
        return [{"kind": a["kind"], "severity": a["severity"], "since": fmt_age((now - a["sinceMs"]) / 1000),
                 "hint": a["hint"]} for a in self.active.values()]

    def full(self, history=False, limit=50):
        now = self.now_ms()
        out = {"active": [{**{k: v for k, v in a.items() if k not in ("source", "key")}, "ageS": (now - a["sinceMs"]) // 1000}
                          for a in self.active.values()]}
        if history:
            out["history"] = list(self.history)[-max(1, limit):]
        out["thresholds"] = {"mcp": dict(self.config)}
        return out

    def dismiss(self, kind, seconds=300.0):
        self.snoozed[kind] = self.clock() + seconds
        return self.clear(kind, "dismissed")

    def configure(self, thresholds=None, disable=(), enable=()):
        """Apply the MCP-side share of a threshold change; returns the keys it did not recognise."""
        rest = {}
        for key, value in (thresholds or {}).items():
            if key in self.config:
                self.config[key] = type(DEFAULTS[key])(value)
            else:
                rest[key] = value
        self.disabled |= set(disable)
        self.disabled -= set(enable)
        return rest

    def attach(self, result):
        """Add the active alerts to a tool result without changing its shape for callers."""
        alerts = self.compact()
        if not alerts:
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                parsed["alerts"] = alerts
                return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
            return result + "\nalerts: " + json.dumps(alerts, separators=(",", ":"), ensure_ascii=False)
        extra = "alerts: " + json.dumps(alerts, separators=(",", ":"), ensure_ascii=False)
        if isinstance(result, (list, tuple)):
            return [*result, extra]
        return [result, extra]
