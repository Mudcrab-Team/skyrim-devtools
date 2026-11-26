#!/usr/bin/env python3.13
"""Live check of the stuck detector on the test PC (SPDX-License-Identifier: GPL-3.0-or-later).

  tests/live/stuck_detector.py --config mac.json --save NAME [--phases wall,nav,navai,pingpong,doorloop]

One visible game session. Each phase provokes one kind of trouble and prints whether the plugin noticed
and how fast (seconds from the start of the command to the first moveStuck, and to the final stop).
  wall      Move.walk straight at the nearest door/wall until the walk fails with `stuck`
  nav       Nav.goto (input) to a point behind that wall
  navai     the same in ai mode
  pingpong  Nav.goto with via points alternating between two spots (moves, never gets closer)
  doorloop  Nav.goto into an interior, out again and in again within a minute
Prints one JSON line {"pass": bool, "details": {...}} last. Run it while holding agent.lock.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skyrim_devtools.use.manager import GameError, GameManager  # noqa: E402


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


class Probe:
    def __init__(self, manager):
        self.events = []
        manager.listeners.append(self.on_event)

    def on_event(self, event):
        if event["type"] in ("moveStuck", "moveUnstuck", "navReplan", "navFailed", "navArrived"):
            data = event.get("data") or {}
            self.events.append({"at": time.monotonic(), "type": event["type"], "source": data.get("source"),
                                "reason": data.get("reason"), "step": data.get("step"), "failures": data.get("failures")})

    def since(self, began):
        return [{**e, "t": round(e.pop("at") - began, 1)} for e in [dict(x) for x in self.events if x["at"] >= began]]


RESULTS = {}


def summarize(label, began, ended, probe, result):
    events = probe.since(began)
    first = next((e["t"] for e in events if e["type"] == "moveStuck"), None)
    RESULTS[label] = {"firstDetectionS": first, "totalS": round(ended - began, 1), "events": events, "result": result}
    out(phase=label, firstDetectionS=first, totalS=round(ended - began, 1), events=events, result=result)


def verdict():
    """Pass criteria: a wall is noticed within 5 s and the command stops within 20 s with `stuck`; a trip
    toward a point behind a wall ends within 25 s (stuck, or a dead end that never pushed on); a ping-pong
    is flagged noProgress and stopped; a door re-entered inside a minute is flagged doorLoop."""
    checks = {}
    wall = RESULTS.get("wall")
    if wall:
        data = (wall["result"] or {}).get("data") or {}
        checks["wall"] = bool(wall["firstDetectionS"] is not None and wall["firstDetectionS"] <= 5.0 and wall["totalS"] <= 20.0
                              and (wall["result"] or {}).get("code") == -32011 and data.get("pos") and "blockers" in data)
    for name in ("nav", "navai"):
        item = RESULTS.get(name)
        if item:
            result = item["result"] or {}
            checks[name] = bool(item["totalS"] <= 25.0 and result.get("state") in ("failed", "arrived") and not result.get("timedOut"))
    ping = RESULTS.get("pingpong")
    if ping:
        reasons = {e.get("reason") for e in ping["events"] if e["type"] == "moveStuck"}
        checks["pingpong"] = bool("noProgress" in reasons and ping["totalS"] <= 45.0 and (ping["result"] or {}).get("state") == "failed")
    loop = RESULTS.get("doorloop")
    if loop:
        checks["doorloop"] = any(e.get("reason") == "doorLoop" for e in loop["events"])
    return {"pass": bool(checks) and all(checks.values()), "details": {"checks": checks, "results": RESULTS}}


async def attempt(manager, method, **params):
    try:
        return await manager.call(method, **params)
    except GameError as error:
        return {"error": str(error)[:400], "code": error.rpc_code, "data": error.rpc_data}


async def wait_nav(manager, limit):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        status = await attempt(manager, "Nav.status")
        if not status.get("active"):
            return status.get("last") or status
        await asyncio.sleep(0.5)
    await attempt(manager, "Nav.cancel")
    return {"timedOut": True}


async def door_target(manager):
    nearby = await manager.call("Nav.nearby", radius=3000, kind="door", limit=10)
    for ref in nearby["refs"]:
        if ref["distance"] > 500:
            return ref
    raise SystemExit("no door 500+ units away to use as a wall")


def bearing(a, b):
    return math.atan2(b[0] - a[0], b[1] - a[1])


async def at_start(manager, start):
    await attempt(manager, "Nav.cancel")
    await attempt(manager, "Move.stop")
    await attempt(manager, "Player.teleport", x=start["x"], y=start["y"], z=start["z"], yawDeg=start["yaw"])
    await asyncio.sleep(3)


async def phase(manager, probe, name, start, wall, args):
    pos = wall["pos"]
    here = [start["x"], start["y"]]
    heading = bearing(here, pos)
    began = time.monotonic()
    if name == "wall":
        await attempt(manager, "Move.turnTo", headingDeg=math.degrees(heading), smooth=False)
        result = await attempt(manager, "Move.walk", direction="forward", durationMs=60000, run=True)
    elif name in ("nav", "navai"):
        behind = {"x": pos[0] + math.sin(heading) * 700, "y": pos[1] + math.cos(heading) * 700, "z": pos[2]}
        reply = await attempt(manager, "Nav.goto", goal=behind, mode="input" if name == "nav" else "ai", timeoutS=90,
                              onCombat="ignore", invulnerable=True)
        result = reply if "error" in reply else await wait_nav(manager, 90)
    elif name == "pingpong":
        a = {"x": start["x"], "y": start["y"], "z": start["z"]}
        # a spot 800 units on the opposite side of the wall, so both ends are open ground
        b = [start["x"] - math.sin(heading) * 800, start["y"] - math.cos(heading) * 800, start["z"]]
        via = [b, [a["x"], a["y"], a["z"]]] * 5
        reply = await attempt(manager, "Nav.goto", goal=a, via=via, mode="input", timeoutS=120, onCombat="ignore",
                              invulnerable=True)
        result = reply if "error" in reply else await wait_nav(manager, 120)
    else:
        raise SystemExit(f"unknown phase {name}")
    summarize(name, began, time.monotonic(), probe, result)


async def doorloop(manager, probe, start):
    nearby = await manager.call("World.nearby", radius=3000, types=["door"], limit=40)
    door = next((r for r in nearby["refs"] if (r.get("destination") or {}).get("interior")), None)
    if not door:
        out(phase="doorloop", skipped="no interior door nearby")
        return
    cell = door["destination"]["cellEditorID"]
    began = time.monotonic()
    legs = []
    for goal in ({"cell": cell}, {"x": start["x"], "y": start["y"], "z": start["z"]}, {"cell": cell}):
        reply = await attempt(manager, "Nav.goto", goal=goal, mode="input", timeoutS=60, onCombat="ignore", invulnerable=True)
        legs.append(reply if "error" in reply else await wait_nav(manager, 60))
        await asyncio.sleep(2)
    summarize("doorloop", began, time.monotonic(), probe,
              [{k: leg.get(k) for k in ("state", "reason", "stuck")} for leg in legs])


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--save", default=None)
    parser.add_argument("--phases", default="wall,nav,navai,pingpong,doorloop")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    manager = GameManager(json.loads(args.config.read_text()))
    probe = Probe(manager)
    try:
        await manager.start(headless=False, save=args.save)
        out(started=True, saves=(await attempt(manager, "Session.listSaves")))
        state = await manager.call("Player.state")
        pos = state["pos"]
        start = {"x": pos["x"], "y": pos["y"], "z": pos["z"], "yaw": (state.get("rotation") or {}).get("z", 0)}
        out(start=start, cell=state.get("cell"))
        wall = await door_target(manager)
        out(wall=wall)
        for name in args.phases.split(","):
            await at_start(manager, start)
            if name == "doorloop":
                await doorloop(manager, probe, start)
            else:
                await phase(manager, probe, name, start, wall, args)
    finally:
        print(json.dumps(verdict(), default=str), flush=True)
        if not args.keep:
            await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
