"""A stand-in for the plugin's Recording.*, Perf.* and Console methods, for tests and live-script dry runs.

It walks a simulated player in a circle, records it the way Recording.stop writes it, and replays it with a little
position noise. `set GameHour to H` in a console command changes the simulated frame time, so an A/B bench of two
hours has a known true difference.
"""

import math
import random
import re


class FakeGame:
    def __init__(self, seed=1, noise=3.0, frame_noise_ms=0.15, hour_cost_ms=0.05, fail_replays=()):
        self.rng = random.Random(seed)
        self.noise = noise
        self.frame_noise_ms = frame_noise_ms
        self.hour_cost_ms = hour_cost_ms
        self.fail_replays = set(fail_replays)
        self.docs = {}
        self.calls = []
        self.recording = None
        self.replays = 0
        self.polls = 0
        self.active = None
        self.last = None
        self.hour = 12.0
        self.trace = None
        self.hitch_ms = None
        self.methods = {"Recording.start", "Recording.stop", "Recording.replay", "Recording.status", "Recording.list",
                        "Recording.read", "Recording.configure", "Recording.replayTrace", "Recording.stopReplay", "Perf.reset", "Perf.frameStats",
                        "Perf.hitches", "Perf.configure", "Console.execute"}

    def has_method(self, name):
        return name in self.methods

    def circle(self, seconds, interval_ms):
        rows = []
        for i in range(int(seconds * 1000 / interval_ms) + 1):
            t = i * interval_ms
            angle = t / 1000.0 / 10.0
            rows.append([t, round(1000 * math.cos(angle), 2), round(1000 * math.sin(angle), 2), 100.0,
                         round(math.degrees(angle) % 360, 2), 0.0, "thirdPerson", 0, 0, 0, 0, 0])
        return rows

    def make_doc(self, name, seconds=60, interval_ms=100, age_ms=3000):
        return {"format": "skyrim-devtools-recording", "version": 1,
                "meta": {"name": name, "intervalMs": interval_ms, "durationMs": seconds * 1000},
                "entry": {"kind": "save", "save": "Fixture", "ageMs": age_ms, "gameHour": 12.0},
                "events": [{"t": 10000, "type": "command", "command": "set timescale to 20", "source": "api"}], "samples": self.circle(seconds, interval_ms)}

    async def call(self, method, timeout=None, **params):
        self.calls.append((method, params))
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            raise RuntimeError(f"fake game has no {method}")
        return handler(**params)

    def m_Recording_configure(self, **options):
        return options

    def m_Recording_start(self, name="rec", intervalMs=100, **_):
        self.recording = name
        return {"name": name, "entry": {"kind": "save", "save": "Fixture", "ageMs": 3000}}

    def m_Recording_stop(self):
        name, self.recording = self.recording, None
        self.docs[name] = self.make_doc(name)
        return {"name": name, "path": f"{name}.json", "samples": len(self.docs[name]["samples"]), "durationMs": 60000}

    def m_Recording_list(self):
        return {"recordings": [{"name": n, "path": f"{n}.json", "durationMs": d["meta"]["durationMs"]} for n, d in self.docs.items()]}

    def m_Recording_read(self, path=None, name=None):
        return self.docs[(path or name).removesuffix(".json")]

    def m_Recording_replay(self, path, beforeRun=(), **_):
        self.replays += 1
        for command in beforeRun:
            self.m_Console_execute(command=command)
        self.active = {"id": self.replays, "phase": "restore"}
        self.polls = 0
        self.doc = self.docs[path.removesuffix(".json")]
        return {"id": self.replays, "phase": "restore"}

    def m_Recording_status(self):
        if self.active:
            self.polls += 1
            if self.polls == 2:
                self.active["phase"] = "run"
            if self.polls >= 4:
                run_id, self.active = self.active["id"], None
                ok = run_id not in self.fail_replays
                rows = self.doc["samples"]
                self.trace = {"samples": [[r[0], r[1] + self.rng.gauss(0, self.noise), r[2] + self.rng.gauss(0, self.noise), r[3], "0"]
                                          for r in rows]}
                self.last = {"id": run_id, "finished": ok, "durationMs": rows[-1][0], "tier": "anchored",
                             "drift": {"medianUnits": 1.0}, "error": None if ok else "restoreTimeout: step waitLoad"}
        return {"replay": self.active, "lastReplay": self.last}

    def m_Recording_replayTrace(self):
        return self.trace

    def m_Recording_stopReplay(self):
        self.active = None
        return {"stopped": True}

    def m_Console_execute(self, command, target=None):
        match = re.match(r"set GameHour to ([\d.]+)", command)
        if match:
            self.hour = float(match.group(1))
        return {"command": command, "lines": []}

    def m_Perf_reset(self):
        return {}

    def m_Perf_configure(self, hitchMs=None, **_):
        self.hitch_ms = hitchMs
        return {"hitchMs": hitchMs}

    def m_Perf_frameStats(self, window=None):
        p50 = 16.6 + self.hour * self.hour_cost_ms + self.rng.gauss(0, self.frame_noise_ms)
        return {"count": 3600, "meanMs": p50 + 0.3, "p50Ms": p50, "p95Ms": p50 + 2.0, "p99Ms": p50 + 5.0 + self.rng.gauss(0, 0.3),
                "maxMs": p50 + 20, "fps": 1000.0 / p50, "hitches": int(abs(self.rng.gauss(2, 1)))}

    def m_Perf_hitches(self, limit=None, **_):
        return {"hitches": []}
