"""Health of the game process, read from the host rather than from the plugin.

A hung main thread cannot answer a plugin call, so the numbers come from the operating system: threads, handles,
memory, CPU and whether the main window still pumps messages. Long sessions have shown the thread count growing
steadily until the game stops answering, so the count and its growth rate are checked against thresholds and
turned into warnings that ride on the status reply (the alerts layer can raise them under the same codes).
"""

from collections import deque

DEFAULTS = {
    "healthThreadWarn": 1500,
    "healthThreadGrowthPerMin": 60,
    "healthGrowthWindowS": 600,
    "healthGrowthMinSpanS": 60,
    "healthRestartAfterMin": 30,
}
MB = 1024 * 1024


def parse_health(raw):
    """Normalise the host's report; None when the game is not running or the report is unusable."""
    if not isinstance(raw, dict) or not raw.get("running") or not isinstance(raw.get("threads"), int):
        return None

    def megabytes(key):
        value = raw.get(key)
        return round(value / MB, 1) if isinstance(value, (int, float)) else None

    return {
        "threads": raw["threads"],
        "handles": raw.get("handles"),
        "privateMB": megabytes("privateBytes"),
        "workingSetMB": megabytes("workingSetBytes"),
        "cpuCores": raw.get("cpuCores"),
        "cpuSeconds": raw.get("cpuSeconds"),
        "responding": raw.get("responding"),
        "uptimeS": raw.get("uptimeSeconds"),
    }


def growth_per_min(samples, window_s, min_span_s):
    """Threads gained per minute across the recent samples (a list of (time, threads)), or None if too short."""
    if len(samples) < 2:
        return None
    newest_time, newest = samples[-1]
    recent = [s for s in samples if newest_time - s[0] <= window_s]
    first_time, first = recent[0]
    span = newest_time - first_time
    if span < min_span_s:
        return None
    return (newest - first) * 60.0 / span


def evaluate(health, samples, config=None):
    """Warnings for one health sample given the recent (time, threads) history; each is {code, severity, message}."""
    settings = dict(DEFAULTS, **(config or {}))
    warnings = []
    if health is None:
        return warnings
    if health.get("responding") is False:
        warnings.append({"code": "notResponding", "severity": "error",
                         "message": "the game window is not answering; it is probably hung: game_stop and game_start"})
    if health["threads"] >= settings["healthThreadWarn"]:
        warnings.append({"code": "threadCount", "severity": "warn",
                         "message": f"the game has {health['threads']} threads (limit {settings['healthThreadWarn']}); "
                                    "past this the game has hung within minutes: save, game_stop, game_start"})
    rate = growth_per_min(samples, settings["healthGrowthWindowS"], settings["healthGrowthMinSpanS"])
    if rate is not None and rate >= settings["healthThreadGrowthPerMin"]:
        warnings.append({"code": "threadGrowth", "severity": "warn",
                         "message": f"threads grow by {rate:.0f} per minute; the game degrades and hangs "
                                    "when this keeps up: plan a restart soon"})
    uptime = health.get("uptimeS")
    if isinstance(uptime, (int, float)) and uptime >= settings["healthRestartAfterMin"] * 60:
        warnings.append({"code": "longSession", "severity": "info",
                         "message": f"the game has run {uptime / 60:.0f} min; sessions degrade after about "
                                    f"{settings['healthRestartAfterMin']} min, restart at a quiet moment"})
    return warnings


class HealthWatch:
    """Keeps the recent thread counts so a growth rate can be judged, and the latest warnings."""

    def __init__(self, config=None, clock=None):
        import time

        self.config = dict(DEFAULTS, **{k: v for k, v in (config or {}).items() if k in DEFAULTS})
        self.clock = clock or time.monotonic
        self.samples = deque(maxlen=720)
        self.latest = None
        self.warnings = []

    def record(self, raw):
        """Feed one host report; returns the normalised sample with `warnings`, or None when unusable."""
        health = parse_health(raw)
        if health is None:
            self.latest, self.warnings = None, []
            return None
        self.samples.append((self.clock(), health["threads"]))
        self.warnings = evaluate(health, list(self.samples), self.config)
        health["warnings"] = self.warnings
        self.latest = health
        return health
