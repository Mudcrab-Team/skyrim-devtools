#!/usr/bin/env python3.13
"""Wall-contact and level measurements for navigation (SPDX-License-Identifier: GPL-3.0-or-later).

Runs under skyrim-devtools-livetest on the shared session (`async def run(ctx)`), or standalone:

  python3.13 tests/live/nav_clearance.py --config mac.json [--save Fixture] [--cases doors,bridge,stairs,tunnel] [--passes 5]
  python3.13 tests/live/nav_clearance.py --dry-run

Every pass is a Nav.goto; Nav.status `stalls` (speed under 40 units/s episodes) says how much time went into
scraping walls:
  doors   walk to each nearby interior door with activate, through it, and back out
  bridge  teleport to the nearest model with "bridge" in its name (up to 20000 units away), walk 500 units
          along it and back; the goal height is the deck, so ending under it fails the level check
  stairs  walk from the start to the nearest "stair" model
  tunnel  after entering the first interior, walk to the farthest named reference and back
Prints {"pass": bool, "details": {...}} last. A pass needs every leg arrived, stall time at most MAX_STALL_S,
and bridge/stairs legs ending within 50 units of the goal height.
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

MAX_STALL_S = 1.0  # per leg; the old behaviour scraped a wall for 1-2 s at each opening
LEVEL_TOLERANCE = 50.0


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


def xyz(pos):
    """Player.state and Nav.status positions: a list [x, y, z] (a dict with x, y, z is accepted too)."""
    if isinstance(pos, dict):
        return [pos["x"], pos["y"], pos["z"]]
    return [pos[0], pos[1], pos[2]]


class Bench:
    def __init__(self, call):
        self.call = call
        self.legs = []
        self.skipped = []

    async def attempt(self, method, **params):
        try:
            return await self.call(method, **params)
        except Exception as error:
            return {"error": str(error)[:300]}

    async def leg(self, label, goal, limit=120, **options):
        began = time.monotonic()
        reply = await self.attempt("Nav.goto", goal=goal, mode="input", timeoutS=limit, onCombat="ignore", invulnerable=True, **options)
        last = reply
        if "error" not in reply:
            while time.monotonic() - began < limit + 10:
                await asyncio.sleep(0.4)
                status = await self.attempt("Nav.status")
                if "error" not in status and not status.get("active"):
                    last = status.get("last") or status
                    break
            else:
                await self.attempt("Nav.cancel")
        stalls = last.get("stalls") or {}
        pos, goal_pos = last.get("pos"), last.get("goalPos")
        entry = {"leg": label, "state": last.get("state") or last.get("error"), "stallS": stalls.get("totalS") or 0.0,
                 "unsticks": last.get("unsticks") or 0, "timeS": round(time.monotonic() - began, 1),
                 "z": xyz(pos)[2] if pos else None, "goalZ": xyz(goal_pos)[2] if goal_pos else None,
                 "reason": last.get("reason")}
        self.legs.append(entry)
        out(**entry)

    async def teleport(self, start):
        await self.attempt("Nav.cancel")
        await self.attempt("Player.teleport", x=start[0], y=start[1], z=start[2])
        await asyncio.sleep(3)

    async def doors(self, passes):
        nearby = await self.call("World.nearby", radius=3500, types=["door"], limit=60)
        doors = [r for r in nearby["refs"] if (r.get("destination") or {}).get("interior") and r["destination"].get("doorRefID")]
        wanted = ("trader", "sleeping giant", "inn")
        doors.sort(key=lambda r: (not any(w in json.dumps(r).lower() for w in wanted), r.get("distance", 0)))
        for door in doors[:2]:
            name = door.get("name")
            for n in range(passes):
                await self.leg(f"door {name} in #{n + 1}", {"refID": door["refID"]}, activate=True, limit=90)
                await asyncio.sleep(3)
                await self.leg(f"door {name} out #{n + 1}", {"refID": door["destination"]["doorRefID"]}, activate=True, limit=60)
                await asyncio.sleep(3)
        if not doors:
            self.skipped.append("doors: no interior door within 3500 units")

    async def model(self, name, contains, start, passes, radius):
        found = await self.call("World.models", contains=contains, radius=radius, limit=40)
        refs = found.get("refs") or []
        if not refs:
            self.skipped.append(f"{name}: no {contains} model within {radius} units")
            return
        target = refs[0]
        pos = xyz(target["pos"])
        out(case=name, target={k: target.get(k) for k in ("refID", "model", "distance", "pos", "angleDeg")})
        if name == "bridge":
            heading = math.radians((target.get("angleDeg") or [0, 0, 0])[2] if isinstance(target.get("angleDeg"), list) else 0)
            far = [pos[0] + math.sin(heading) * 500, pos[1] + math.cos(heading) * 500, pos[2]]
            for n in range(passes):
                await self.attempt("Player.teleport", refID=target["refID"])
                await asyncio.sleep(6)
                await self.leg(f"bridge across #{n + 1}", {"x": far[0], "y": far[1], "z": far[2]}, limit=60)
                await self.leg(f"bridge back #{n + 1}", {"x": pos[0], "y": pos[1], "z": pos[2]}, limit=60)
            return
        for n in range(passes):
            await self.teleport(start)
            await self.leg(f"{name} #{n + 1}", {"x": pos[0], "y": pos[1], "z": pos[2]}, limit=90)

    async def tunnel(self, passes):
        nearby = await self.call("World.nearby", radius=3500, types=["door"], limit=60)
        door = next((r for r in nearby["refs"] if (r.get("destination") or {}).get("interior")), None)
        if not door:
            self.skipped.append("tunnel: no interior door")
            return
        await self.leg("tunnel enter", {"refID": door["refID"]}, activate=True, limit=90)
        await asyncio.sleep(4)
        here = xyz((await self.call("Player.state"))["pos"])
        refs = (await self.call("Nav.nearby", radius=6000, kind="any", limit=60))["refs"]
        far = max(refs, key=lambda r: r["distance"], default=None)
        if not far:
            self.skipped.append("tunnel: nothing named inside")
            return
        goal = xyz(far["pos"])
        for n in range(passes):
            await self.leg(f"tunnel in #{n + 1}", {"x": goal[0], "y": goal[1], "z": goal[2]}, limit=120)
            await self.leg(f"tunnel back #{n + 1}", {"x": here[0], "y": here[1], "z": here[2]}, limit=120)


def verdict(bench):
    bad = []
    for leg in bench.legs:
        if leg["state"] != "arrived":
            bad.append({**leg, "why": "not arrived"})
        elif leg["stallS"] > MAX_STALL_S:
            bad.append({**leg, "why": f"stalled {leg['stallS']} s"})
        elif leg["leg"].startswith(("bridge", "stairs")) and leg["z"] is not None and leg["goalZ"] is not None \
                and abs(leg["z"] - leg["goalZ"]) > LEVEL_TOLERANCE:
            bad.append({**leg, "why": "ended on another level"})
    stall = [leg["stallS"] for leg in bench.legs]
    return {"pass": bool(bench.legs) and not bad, "details": {
        "legs": len(bench.legs), "failures": bad, "skipped": bench.skipped,
        "meanStallS": round(sum(stall) / len(stall), 2) if stall else None, "maxStallS": max(stall) if stall else None,
        "all": bench.legs}}


async def run(ctx, cases=("doors", "bridge", "stairs", "tunnel"), passes=5):
    bench = Bench(ctx.call)
    start = xyz((await ctx.call("Player.state"))["pos"])
    try:
        for name in cases:
            await bench.attempt("Nav.cancel")
            await bench.teleport(start)
            if name == "doors":
                await bench.doors(passes)
            elif name == "bridge":
                await bench.model("bridge", "bridge", start, min(passes, 3), 20000)
            elif name == "stairs":
                await bench.model("stairs", "stair", start, passes, 6000)
            elif name == "tunnel":
                await bench.tunnel(passes)
    finally:
        await bench.attempt("Nav.cancel")
    return verdict(bench)


class DryCtx:
    """Answers the calls the script makes, so it can be exercised without a game."""

    def __init__(self):
        self.calls = []

    async def call(self, method, timeout=None, **params):
        self.calls.append(method)
        if method == "Player.state":
            return {"pos": [0.0, 0.0, 0.0]}
        if method == "World.nearby":
            return {"refs": [{"refID": "0001", "name": "Wooden Door", "distance": 100.0,
                              "destination": {"interior": True, "doorRefID": "0002"}}]}
        if method == "World.models":
            return {"refs": [{"refID": "0003", "pos": [0.0, 0.0, 10.0], "angleDeg": [0, 0, 90]}]}
        if method == "Nav.nearby":
            return {"refs": [{"refID": "0004", "pos": [500.0, 0.0, 0.0], "distance": 500.0}]}
        if method == "Nav.status":
            return {"active": False, "last": {"state": "arrived", "stalls": {"totalS": 0.0}, "pos": [0, 0, 10.0], "goalPos": [0, 0, 10.0]}}
        return {}


async def standalone(args):
    cases = tuple(args.cases.split(","))
    if args.dry_run:
        ctx = DryCtx()
        result = await run(ctx, cases, 1)
        print(json.dumps({"dryRun": True, "calls": len(ctx.calls), "pass": result["pass"], "legs": result["details"]["legs"]}), flush=True)
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
        result = await run(ctx, cases, args.passes)
    finally:
        await manager.stop()
    print(json.dumps(result, default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--cases", default="doors,bridge,stairs,tunnel")
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if not options.dry_run and not options.config:
        parser.error("--config is required without --dry-run")
    asyncio.run(standalone(options))
