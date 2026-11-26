#!/usr/bin/env python3.13
"""Frame-rate A/B on the test PC: one game session, a list of load phases, one reading per phase (SPDX-License-Identifier: GPL-3.0-or-later).

  tools/perf_session.py --config mac.json --phases idle:30,walk:30,poll:60,scan:60,nav:120,capture:60 [--save NAME] [--windowed-ini]

Phases are name:seconds (capture records while running navloop, which cycles Riverwood, its trader and
Bleak Falls Barrow with status polling and nearby scans). idle does nothing, walk holds Move.walk with turns, poll reads Player.state, Quest.active
and Nav.status every 0.5 s, scan adds World.nearby and World.models every 3 s, nav runs Nav.goto to a place while
polling, capture records the desktop. Each phase resets Perf and prints fps, p50/p99, hitches and the plugin's own
slow calls. Run it while holding agent.lock (tools/pc-build lock).
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from skyrim_devtools.use.capture import CaptureHost  # noqa: E402
from skyrim_devtools.use.manager import GameManager  # noqa: E402


async def attempt(manager, method, **params):
    try:
        return await manager.call(method, **params)
    except Exception as error:
        return {"error": str(error)[:160]}


async def run_phase(manager, name, seconds, args):
    end = time.monotonic() + seconds
    if name == "idle":
        await asyncio.sleep(seconds)
    elif name == "walk":
        turn = 0
        while time.monotonic() < end:
            await attempt(manager, "Player.look", yawDeg=40 if turn % 2 else -40)
            await attempt(manager, "Move.walk", direction="forward", durationMs=2500)
            turn += 1
            await asyncio.sleep(2.6)
        await attempt(manager, "Move.stop")
    elif name in {"poll", "scan"}:
        tick = 0
        while time.monotonic() < end:
            await attempt(manager, "Player.state")
            await attempt(manager, "Quest.active")
            await attempt(manager, "Nav.status")
            if name == "scan" and tick % 6 == 0:
                await attempt(manager, "World.nearby", radius=3000, limit=200)
                await attempt(manager, "World.models", radius=3000)
            tick += 1
            await asyncio.sleep(0.5)
    elif name == "nav":
        await attempt(manager, "Nav.goto", goal={"place": args.place}, mode="input", timeoutS=seconds)
        while time.monotonic() < end:
            status = await attempt(manager, "Nav.status")
            await attempt(manager, "Player.state")
            if not status.get("active"):
                break
            await asyncio.sleep(1.0)
        await attempt(manager, "Nav.cancel")
    elif name == "navloop":
        goals = [{"place": args.place}, {"cell": "RiverwoodRiverwoodTrader"}, {"place": args.place}, {"place": "Bleak Falls Barrow"}]
        index, tick = 0, 0
        while time.monotonic() < end:
            reply = await attempt(manager, "Nav.goto", goal=goals[index % len(goals)], mode="input", timeoutS=200,
                                  invulnerable=True, onCombat="ignore")
            index += 1
            while time.monotonic() < end:
                status = await attempt(manager, "Nav.status")
                await attempt(manager, "Player.state")
                await attempt(manager, "Quest.active")
                if tick % 4 == 0:
                    await attempt(manager, "World.nearby", radius=2000, limit=100)
                tick += 1
                if not status.get("active") or "error" in reply:
                    break
                await asyncio.sleep(1.0)
        await attempt(manager, "Nav.cancel")
    else:
        raise SystemExit(f"unknown phase {name}")


async def reading(manager, name, started_frame):
    stats = await manager.call("Perf.frameStats")
    hitches = await manager.call("Perf.hitches", limit=15)
    keep = ("count", "fps", "p50Ms", "p95Ms", "p99Ms", "maxMs")
    summary = {k: stats.get(k) for k in keep}
    worst = sorted(hitches["hitches"], key=lambda h: -h["ms"])[:5]
    return {"phase": name, **summary, "hitchesLogged": hitches["logged"], "worst": worst, "slowCalls": hitches.get("slowCalls", {})}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--phases", default="idle:30,walk:30")
    parser.add_argument("--save", default=None)
    parser.add_argument("--place", default="Riverwood")
    parser.add_argument("--settle", type=float, default=20)
    parser.add_argument("--keep", action="store_true", help="leave the game running")
    args = parser.parse_args()
    manager = GameManager(json.loads(args.config.read_text()))
    capture = CaptureHost(manager.host)
    try:
        info = await manager.start(headless=False, save=args.save)
        print(json.dumps({"started": True, "save": args.save, "info": {k: info.get(k) for k in ("phase", "mode")}}), flush=True)
        print(json.dumps({"window": await attempt(manager, "Window.info")}), flush=True)
        await attempt(manager, "Perf.configure", hitchLogMs=100, ringSize=200000)
        await asyncio.sleep(args.settle)
        for item in args.phases.split(","):
            name, _, seconds = item.partition(":")
            seconds = float(seconds or 30)
            recording = name == "capture"
            if recording:
                state = await capture.action("capture-start", manager.session_id, "-Fps 30 -Width 1920", timeout=40)
                print(json.dumps({"capture": state.get("phase"), "encoder": state.get("encoder")}), flush=True)
                name = "navloop"
            await attempt(manager, "Perf.reset")
            await run_phase(manager, name, seconds, args)
            result = await reading(manager, ("capture+" if recording else "") + name, 0)
            print(json.dumps(result), flush=True)
            if recording:
                await capture.action("capture-stop", manager.session_id, timeout=60)
    finally:
        if not args.keep:
            await manager.stop()


asyncio.run(main())
