#!/usr/bin/env python3.13
"""Live check of the stuck detector (SPDX-License-Identifier: GPL-3.0-or-later).

Runs under skyrim-devtools-livetest on the shared session (`async def run(ctx)`), or standalone:

  python3.13 tests/live/stuck_detector.py --config mac.json [--save Fixture] [--phases wall,nav,navai,pingpong,doorloop]
  python3.13 tests/live/stuck_detector.py --dry-run

Phases (each provokes one kind of trouble and records whether and how fast the plugin noticed):
  wall      Move.walk straight at the nearest door from 150 units away until it fails with `stuck`
  nav       Nav.goto (input) to a point behind that door
  navai     the same in ai mode
  pingpong  Nav.goto with 30 via points alternating between two open spots (moves, never closer)
  doorloop  Nav.goto into an interior door, out of it and in again inside a minute
Prints {"pass": bool, "details": {...}} last.
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

NAV_LIMIT_S = 25.0
WALL_DETECT_S = 5.0
WALL_STOP_S = 20.0
PINGPONG_STOP_S = 45.0


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


def xyz(pos):
    """Player.state position: a list [x, y, z] (a dict with x, y, z is accepted too)."""
    if isinstance(pos, dict):
        return [pos["x"], pos["y"], pos["z"]]
    return [pos[0], pos[1], pos[2]]


class Probe:
    KINDS = ("moveStuck", "moveUnstuck", "navReplan", "navFailed", "navArrived")

    def __init__(self, manager):
        self.events = []
        manager.listeners.append(self.on_event)
        self.manager = manager

    def detach(self):
        if self.on_event in self.manager.listeners:
            self.manager.listeners.remove(self.on_event)

    def on_event(self, event):
        if event["type"] in self.KINDS:
            data = event.get("data") or {}
            self.events.append({"at": time.monotonic(), "type": event["type"], "source": data.get("source"),
                                "reason": data.get("reason") if event["type"] != "navFailed" else None,
                                "step": data.get("step"), "failures": data.get("failures")})

    def since(self, began):
        return [{**{k: v for k, v in e.items() if k != "at"}, "t": round(e["at"] - began, 1)} for e in self.events if e["at"] >= began]


class Session:
    def __init__(self, call, probe):
        self.call = call
        self.probe = probe
        self.results = {}

    async def attempt(self, method, **params):
        try:
            return await self.call(method, **params)
        except Exception as error:
            return {"error": str(error)[:400], "code": getattr(error, "rpc_code", None), "data": getattr(error, "rpc_data", None)}

    async def wait_nav(self, limit):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            status = await self.attempt("Nav.status")
            if "error" not in status and not status.get("active"):
                return status.get("last") or status
            await asyncio.sleep(0.4)
        await self.attempt("Nav.cancel")
        return {"timedOut": True}

    def record(self, label, began, result):
        events = self.probe.since(began)
        first = next((e["t"] for e in events if e["type"] == "moveStuck"), None)
        self.results[label] = {"firstDetectionS": first, "totalS": round(time.monotonic() - began, 1), "events": events, "result": result}
        out(phase=label, firstDetectionS=first, totalS=self.results[label]["totalS"], events=events)

    async def place(self, x, y, z, heading_deg):
        await self.attempt("Nav.cancel")
        await self.attempt("Move.stop")
        await self.attempt("Player.teleport", x=x, y=y, z=z, yawDeg=heading_deg)
        await asyncio.sleep(2.5)
        await self.attempt("Move.turnTo", headingDeg=heading_deg, smooth=False)


def verdict(results):
    checks = {}
    wall = results.get("wall")
    if wall:
        result = wall["result"] or {}
        data = result.get("data") or {}
        checks["wall"] = bool(wall["firstDetectionS"] is not None and wall["firstDetectionS"] <= WALL_DETECT_S
                              and wall["totalS"] <= WALL_STOP_S and result.get("code") == -32011 and data.get("pos") and "blockers" in data)
    for name in ("nav", "navai"):
        item = results.get(name)
        if item:
            result = item["result"] or {}
            checks[name] = bool(item["totalS"] <= NAV_LIMIT_S and result.get("state") in ("failed", "arrived") and not result.get("timedOut"))
    ping = results.get("pingpong")
    if ping:
        reasons = {e.get("reason") for e in ping["events"] if e["type"] == "moveStuck"}
        checks["pingpong"] = bool("noProgress" in reasons and ping["totalS"] <= PINGPONG_STOP_S and (ping["result"] or {}).get("state") == "failed")
    loop = results.get("doorloop")
    if loop:
        checks["doorloop"] = any(e.get("reason") == "doorLoop" for e in loop["events"])
    return {"pass": bool(checks) and all(checks.values()), "details": {"checks": checks, "results": results}}


async def phases(s, names, start):
    nearby = await s.call("Nav.nearby", radius=3000, kind="door", limit=10)
    wall = next((r for r in nearby["refs"] if r["distance"] > 200), None)
    if not wall:
        return {"pass": False, "details": {"error": "no door within 3000 units to use as a wall"}}
    door = wall["pos"]
    heading = math.atan2(door[0] - start[0], door[1] - start[1])
    near = [door[0] - math.sin(heading) * 150, door[1] - math.cos(heading) * 150, door[2]]
    out(wall=wall, near=near)
    for name in names:
        if name == "doorloop":
            await doorloop(s, start)
            continue
        if name == "pingpong":
            await s.place(*start, math.degrees(heading))
            other = [start[0] - math.sin(heading) * 800, start[1] - math.cos(heading) * 800, start[2]]
            via = ([other, list(start)]) * 15
            began = time.monotonic()
            reply = await s.attempt("Nav.goto", goal={"x": start[0], "y": start[1], "z": start[2]}, via=via, mode="input",
                                    timeoutS=120, onCombat="ignore", invulnerable=True)
            s.record(name, began, reply if "error" in reply else await s.wait_nav(120))
            continue
        await s.place(*near, math.degrees(heading))
        began = time.monotonic()
        if name == "wall":
            result = await s.attempt("Move.walk", direction="forward", durationMs=60000, run=True)
        else:
            behind = {"x": door[0] + math.sin(heading) * 700, "y": door[1] + math.cos(heading) * 700, "z": door[2]}
            reply = await s.attempt("Nav.goto", goal=behind, mode="input" if name == "nav" else "ai", timeoutS=90,
                                    onCombat="ignore", invulnerable=True)
            result = reply if "error" in reply else await s.wait_nav(90)
        s.record(name, began, result)
    return None


async def doorloop(s, start):
    nearby = await s.call("World.nearby", radius=3500, types=["door"], limit=60)
    candidates = [r for r in nearby["refs"] if (r.get("destination") or {}).get("interior") and r["destination"].get("doorRefID")]
    out(phase="doorloop", candidates=[{k: r.get(k) for k in ("refID", "name", "locked", "distance", "destination")} for r in candidates[:8]])
    # a locked house door cannot be entered, so the trip would fail for a reason unrelated to the loop detector
    door = next((r for r in candidates if not r.get("locked")), None)
    if not door:
        out(phase="doorloop", skipped="no interior door nearby")
        return
    await s.place(*start, 0)
    began = time.monotonic()
    legs = []
    for goal in ({"refID": door["refID"]}, {"refID": door["destination"]["doorRefID"]}, {"refID": door["refID"]}):
        reply = await s.attempt("Nav.goto", goal=goal, mode="input", timeoutS=40, onCombat="ignore", invulnerable=True, activate=True)
        legs.append(reply if "error" in reply else await s.wait_nav(40))
        await asyncio.sleep(1)
    s.record("doorloop", began, [{k: leg.get(k) for k in ("state", "reason")} for leg in legs])


async def run(ctx, names=("wall", "nav", "navai", "pingpong", "doorloop")):
    probe = Probe(ctx.manager)
    s = Session(ctx.call, probe)
    try:
        state = await ctx.call("Player.state")
        start = xyz(state["pos"])
        early = await phases(s, names, start)
        if early:
            return early
    finally:
        probe.detach()
        await s.attempt("Nav.cancel")
        await s.attempt("Move.stop")
    return verdict(s.results)


class DryManager:
    """Answers the calls the script makes, with a player that is never stuck, to exercise the script offline."""

    def __init__(self):
        self.listeners = []
        self.calls = []

    async def call(self, method, timeout=None, **params):
        self.calls.append(method)
        if method == "Player.state":
            return {"pos": [0.0, 0.0, 0.0]}
        if method == "Nav.nearby":
            return {"refs": [{"refID": "0001", "pos": [0.0, 500.0, 0.0], "distance": 500.0}]}
        if method == "World.nearby":
            return {"refs": [{"refID": "0001", "destination": {"interior": True, "doorRefID": "0002"}}]}
        if method == "Nav.status":
            return {"active": False, "last": {"state": "arrived"}}
        return {}


class DryCtx:
    def __init__(self):
        self.manager = DryManager()
        self.call = self.manager.call


async def standalone(args):
    if args.dry_run:
        ctx = DryCtx()
        result = await run(ctx, tuple(args.phases.split(",")))
        print(json.dumps({"dryRun": True, "calls": len(ctx.manager.calls), "verdict": result["pass"]}), flush=True)
        return
    from skyrim_devtools.use.manager import GameManager

    manager = GameManager(json.loads(Path(args.config).read_text()))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.manager = manager
    ctx.call = manager.call
    try:
        await manager.start(headless=False, save=args.save)
        result = await run(ctx, tuple(args.phases.split(",")))
    finally:
        await manager.stop()
    print(json.dumps(result, default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--phases", default="wall,nav,navai,pingpong,doorloop")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if not options.dry_run and not options.config:
        parser.error("--config is required without --dry-run")
    asyncio.run(standalone(options))
