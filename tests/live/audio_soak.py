#!/usr/bin/env python3.13
"""Long-session soak: does the game's thread count stay flat once it has an audio output device
(SPDX-License-Identifier: GPL-3.0-or-later).

  tests/live/audio_soak.py --config mac.json --save NAME [--minutes 45] [--interval 60] [--disable-audio]

One visible game session. The character walks and idles in an exterior without fighting while the host
process (SkyrimSE) is sampled every --interval seconds: thread count, handles, private MB, CPU in cores, and
frame rate from Perf.frameStats. Host-side counters come from Get-Process over SSH, so they keep working when
the game stops answering; fps is None for such samples.

Pass: thread count stays under 600, growth over the last 30 min is below 10 per minute, the process never
stops responding or vanishes. Prints one JSON line {"pass": bool, "details": {...}} last, with the
per-sample series. Run it while holding the PC lock.

--disable-audio sets the host config key disableAudio for the run (restored afterwards) so the engine's audio
layer is off; the same pass rule then shows how much of the growth is not audio.
"""
import argparse
import asyncio
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skyrim_devtools.use.manager import GameManager  # noqa: E402

MAX_THREADS = 600
MAX_GROWTH_PER_MIN = 10.0
GROWTH_WINDOW_MIN = 30.0
REMOTE_CONFIG = "$env:USERPROFILE\\.local\\skyrim-devtools\\config.json"

SAMPLE_SCRIPT = (
    "$p = Get-Process SkyrimSE -ErrorAction SilentlyContinue | Select-Object -First 1; "
    "if (-not $p) { '{\"alive\":false}' } else { "
    "[pscustomobject]@{alive=$true; threads=$p.Threads.Count; handles=$p.HandleCount; "
    "privateMB=[math]::Round($p.PrivateMemorySize64/1MB); cpuS=[math]::Round($p.TotalProcessorTime.TotalSeconds,1); "
    "responding=[bool]$p.Responding} | ConvertTo-Json -Compress }"
)


def slope_per_min(points):
    """Least-squares slope of (minutes, value) points; 0.0 with fewer than two distinct times."""
    if len(points) < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if spread == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / spread


def evaluate(samples, minutes):
    """samples: dicts with `t` (seconds since start) and host fields. Returns (pass, checks)."""
    alive = [s for s in samples if s.get("alive")]
    threads = [s["threads"] for s in alive if isinstance(s.get("threads"), int)]
    window_start = max(0.0, minutes - GROWTH_WINDOW_MIN) * 60
    tail = [(s["t"] / 60, s["threads"]) for s in alive if s["t"] >= window_start and isinstance(s.get("threads"), int)]
    # a window covering less than half of the intended span says nothing about growth
    span = (tail[-1][0] - tail[0][0]) if len(tail) >= 2 else 0.0
    growth = slope_per_min(tail)
    checks = {
        "samples": len(samples),
        "maxThreads": max(threads) if threads else None,
        "growthPerMinLast30": round(growth, 2),
        "growthWindowMin": round(span, 1),
        "everNotResponding": any(s.get("alive") and s.get("responding") is False for s in samples),
        "processVanished": any(not s.get("alive") for s in samples),
    }
    ok = bool(threads)
    ok = ok and checks["maxThreads"] < MAX_THREADS
    ok = ok and span >= min(GROWTH_WINDOW_MIN, minutes) / 2 and growth < MAX_GROWTH_PER_MIN
    ok = ok and not checks["everNotResponding"] and not checks["processVanished"]
    return ok, checks


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


async def powershell(manager, script, timeout=40):
    host = manager.host
    process = await asyncio.create_subprocess_exec(
        *host.ssh, "-T", host.destination, "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        data, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return ""
    return data.decode(errors="replace").strip()


async def host_sample(manager):
    raw = await powershell(manager, SAMPLE_SCRIPT)
    try:
        return json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError):
        return {"alive": None, "error": raw[:120]}


async def set_audio_disabled(manager, disabled):
    """Edit the host config key read at game start; returns the previous raw value so it can be restored."""
    if disabled:
        script = (f"$f = \"{REMOTE_CONFIG}\"; $c = Get-Content $f -Raw | ConvertFrom-Json; "
                  "$c | Add-Member -NotePropertyName disableAudio -NotePropertyValue $true -Force; "
                  "$c | ConvertTo-Json | Set-Content $f")
    else:
        script = (f"$f = \"{REMOTE_CONFIG}\"; $c = Get-Content $f -Raw | ConvertFrom-Json; "
                  "$c.PSObject.Properties.Remove('disableAudio'); $c | ConvertTo-Json | Set-Content $f")
    await powershell(manager, script)


async def attempt(manager, method, **params):
    try:
        return await manager.call(method, **params)
    except Exception as error:  # a stalled or dying game must not stop the sampling
        return {"error": f"{type(error).__name__}: {str(error)[:120]}"}


class Mover:
    """Walks and idles near the start so the engine streams cells and runs AI without a fight. Returns to the
    start periodically so a long session does not drift into a settlement or a dungeon door."""

    def __init__(self, manager, start, seed=7):
        self.manager = manager
        self.start = start
        self.random = random.Random(seed)
        self.cycles = 0
        self.errors = 0
        self.stop = False

    async def run(self):
        m = self.manager
        while not self.stop:
            heading = self.random.uniform(0, 360)
            for method, params in (
                ("Move.turnTo", {"headingDeg": heading, "smooth": True}),
                ("Move.walk", {"direction": "forward", "durationMs": 12000, "run": False}),
            ):
                reply = await attempt(m, method, **params)
                self.errors += "error" in reply
            await asyncio.sleep(self.random.uniform(5, 15))
            self.cycles += 1
            if self.cycles % 4 == 0:
                s = self.start
                reply = await attempt(m, "Player.teleport", x=s["x"], y=s["y"], z=s["z"], yawDeg=s["yaw"])
                self.errors += "error" in reply
                await asyncio.sleep(3)


async def fps_reading(manager):
    try:
        # a stalled main thread never answers; the host-side counters are the record then
        stats = await asyncio.wait_for(attempt(manager, "Perf.frameStats", window=10), 20)
    except asyncio.TimeoutError:
        return None
    frame = stats.get("stats", stats) if isinstance(stats, dict) else {}
    for key in ("fps", "avgFps"):
        if isinstance(frame.get(key), (int, float)):
            return round(frame[key], 1)
    return None


async def soak(args):
    manager = GameManager(json.loads(args.config.read_text()))
    samples = []
    details = {"minutes": args.minutes, "interval": args.interval, "audioDisabled": args.disable_audio}
    mover = None
    mover_task = None
    try:
        await set_audio_disabled(manager, args.disable_audio)
        await manager.start(headless=False, save=args.save)
        state = await manager.call("Player.state")
        pos = state["pos"]
        start = {"x": pos["x"], "y": pos["y"], "z": pos["z"], "yaw": (state.get("rotation") or {}).get("z", 0)}
        details["cell"] = state.get("cell")
        mover = Mover(manager, start)
        mover_task = asyncio.create_task(mover.run())
        began = time.monotonic()
        last_cpu = None
        last_at = None
        while True:
            at = time.monotonic() - began
            sample = await host_sample(manager)
            sample["t"] = round(at)
            if sample.get("alive") and last_cpu is not None and at > last_at:
                sample["cpuCores"] = round((sample["cpuS"] - last_cpu) / (at - last_at), 2)
            if sample.get("alive"):
                last_cpu, last_at = sample["cpuS"], at
                sample["fps"] = await fps_reading(manager)
            samples.append(sample)
            out(sample=sample)
            if not sample.get("alive") and sample.get("alive") is not None:
                break
            if at + args.interval > args.minutes * 60:
                break
            await asyncio.sleep(max(1.0, args.interval - (time.monotonic() - began - at)))
        details["moverCycles"] = mover.cycles
        details["moverErrors"] = mover.errors
    except Exception as error:
        details["error"] = f"{type(error).__name__}: {error}"
    finally:
        if mover:
            mover.stop = True
        if mover_task:
            mover_task.cancel()
        try:
            await set_audio_disabled(manager, False)
            if not args.keep:
                await manager.stop()
        except Exception as error:
            details["cleanupError"] = f"{type(error).__name__}: {error}"
    ok, checks = evaluate(samples, args.minutes)
    details.update(checks)
    details["perMinuteSeries"] = [
        {k: s.get(k) for k in ("t", "threads", "handles", "privateMB", "cpuCores", "fps", "responding", "alive")}
        for s in samples]
    return {"pass": ok and "error" not in details, "details": details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--save", default=None)
    parser.add_argument("--minutes", type=float, default=45)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--disable-audio", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    verdict = asyncio.run(soak(args))
    print(json.dumps(verdict, default=str), flush=True)
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
