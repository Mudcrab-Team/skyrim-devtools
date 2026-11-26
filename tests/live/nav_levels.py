#!/usr/bin/env python3.13
"""Multi-level navigation in Embershard Mine (SPDX-License-Identifier: GPL-3.0-or-later).

Runs under skyrim-devtools-livetest on the shared session (an `async def run(ctx)` module), or standalone:

  python3.13 tests/live/nav_levels.py --config mac.json [--save Fixture] [--passes 3]
  python3.13 tests/live/nav_levels.py --dry-run

The mine has a lower floor and walkways and a wooden bridge 400 units above it; a trip toward the upper level
used to end under it. The script moves to the cell with the console (`World.centerOnCell`), then:
  levels  Nav.plan to four upper/lower targets (north walkway, west floor, west bridge level, west top) and
          records startLevelZ/goalLevelZ and the z range of the path. Gating: the north walkway goal resolves
          to its own level (within 100 of the requested z) and its plan is complete and climbs. The west
          targets are informational: the extending bridge may have no navmesh at all, and that is reported.
  walk    Nav.goto onto the north walkway `passes` times from the entrance; gating: arrived, final z within 60 of
          the walkway height (not the floor below), the plan climbed at least 150 units with no segment steeper
          than 1.5 rise per run (a ramp, not a jump), and the distance travelled at most 3x the plan length.
Positions come from the Embershard playtest (docs/playtests/embershard-mine.md): the north walkway bandits
stood at about (200, 3965, 8535); the west bandits at (-2355, 2274, 8108), (-3221, 2345, 8446), (-3938, 1530, 8606).
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

CELL = "EmbershardMine01"
WALKWAY = (200.0, 3965.0, 8535.0)
WEST = {"westFloor": (-2355.0, 2274.0, 8108.0), "westBridgeLevel": (-3221.0, 2345.0, 8446.0), "westTop": (-3938.0, 1530.0, 8606.0)}
LEVEL_MATCH = 100.0
ARRIVE_Z = 60.0
MIN_CLIMB = 150.0
WANDER_FACTOR = 3.0
MAX_SLOPE = 1.5


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


def xyz(pos):
    """Positions come as lists [x, y, z]; a dict with x, y, z is accepted too."""
    if isinstance(pos, dict):
        return [pos["x"], pos["y"], pos["z"]]
    return [pos[0], pos[1], pos[2]]


class Bench:
    def __init__(self, call):
        self.call = call

    async def attempt(self, method, **params):
        try:
            return await self.call(method, **params)
        except Exception as error:
            return {"error": str(error)[:300]}

    async def enter_mine(self):
        await self.attempt("Nav.cancel")
        reply = await self.attempt("World.centerOnCell", cell=CELL)
        for _ in range(40):
            await asyncio.sleep(1.5)
            state = await self.attempt("Player.state")
            cell = state.get("cell")
            name = (cell.get("editorID") if isinstance(cell, dict) else cell) or ""
            if "error" not in state and CELL.lower() in str(name).lower():
                await asyncio.sleep(3)
                return xyz(state["pos"]), reply
        return None, reply

    async def plan(self, name, target):
        plan = await self.attempt("Nav.plan", x=target[0], y=target[1], z=target[2])
        points = plan.get("points") or []
        zs = [p[2] for p in points]
        info = {"target": name, "requestedZ": target[2], "found": plan.get("found"), "complete": plan.get("complete"),
                "goalOnMesh": plan.get("goalOnMesh"), "startLevelZ": plan.get("startLevelZ"), "goalLevelZ": plan.get("goalLevelZ"),
                "pathZ": [round(min(zs)), round(max(zs))] if zs else None, "length": plan.get("length"), "points": len(points),
                "error": plan.get("error")}
        out(plan=info)
        return info, plan


def climbed_through_ramp(plan, start_z, goal_z):
    """The plan gains the height by walking: it climbs at least MIN_CLIMB overall and no segment is steeper
    than MAX_SLOPE (rise over run), so it follows a ramp or stairs instead of cutting straight up to a point
    that is above the player."""
    points = plan.get("points") or []
    if len(points) < 2 or goal_z - start_z < MIN_CLIMB:
        return False
    for a, b in zip(points, points[1:]):
        run = math.hypot(b[0] - a[0], b[1] - a[1])
        if abs(b[2] - a[2]) > MAX_SLOPE * max(run, 1.0) and abs(b[2] - a[2]) > 60.0:
            return False
    return points[-1][2] - points[0][2] >= MIN_CLIMB


async def run(ctx, passes=3):
    bench = Bench(ctx.call)
    start, entered = await bench.enter_mine()
    if start is None:
        return {"pass": False, "details": {"error": f"did not reach {CELL}", "centerOnCell": entered}}
    out(entered=start)
    checks, details = {}, {"start": start, "plans": [], "walks": []}
    info, plan = await bench.plan("northWalkway", WALKWAY)
    details["plans"].append(info)
    level = info["goalLevelZ"]
    checks["walkwayLevel"] = bool(info["goalOnMesh"] and level is not None and abs(level - WALKWAY[2]) <= LEVEL_MATCH)
    checks["walkwayPlan"] = bool(info["complete"]) and climbed_through_ramp(plan, start[2], WALKWAY[2])
    for name, target in WEST.items():
        info, _ = await bench.plan(name, target)
        level = info["goalLevelZ"]
        info["onItsLevel"] = bool(level is not None and abs(level - target[2]) <= LEVEL_MATCH)
        details["plans"].append(info)
    for n in range(passes):
        await bench.attempt("Nav.cancel")
        await bench.attempt("Player.teleport", x=start[0], y=start[1], z=start[2])
        await asyncio.sleep(3)
        _, plan = await bench.plan(f"northWalkway pass {n + 1}", WALKWAY)
        began = time.monotonic()
        reply = await bench.attempt("Nav.goto", goal={"x": WALKWAY[0], "y": WALKWAY[1], "z": WALKWAY[2]}, mode="input",
                                    timeoutS=150, onCombat="ignore", invulnerable=True)
        last = reply
        if "error" not in reply:
            while time.monotonic() - began < 160:
                await asyncio.sleep(0.5)
                status = await bench.attempt("Nav.status")
                if "error" not in status and not status.get("active"):
                    last = status.get("last") or status
                    break
            else:
                await bench.attempt("Nav.cancel")
        pos = xyz(last["pos"]) if last.get("pos") else None
        length = plan.get("length") or 0.0
        walk = {"pass": n + 1, "state": last.get("state") or last.get("error"), "finalZ": pos[2] if pos else None,
                "timeS": round(time.monotonic() - began, 1), "travelled": last.get("travelled"), "planLength": length,
                "ramp": climbed_through_ramp(plan, start[2], WALKWAY[2]), "reason": last.get("reason"),
                "stallS": (last.get("stalls") or {}).get("totalS")}
        walk["ok"] = bool(walk["state"] == "arrived" and pos and abs(pos[2] - WALKWAY[2]) <= ARRIVE_Z and walk["ramp"]
                          and (last.get("travelled") or 0.0) <= WANDER_FACTOR * max(length, 1.0))
        details["walks"].append(walk)
        out(walk=walk)
    checks["walks"] = bool(details["walks"]) and all(w["ok"] for w in details["walks"])
    await bench.attempt("Nav.cancel")
    return {"pass": all(checks.values()), "details": {"checks": checks, **details}}


class DryCtx:
    """Answers the calls the script makes with a mine whose walkway is a ramp up from the entrance."""

    def __init__(self):
        self.calls = []

    async def call(self, method, timeout=None, **params):
        self.calls.append(method)
        if method == "Player.state":
            return {"pos": [0.0, 0.0, 8085.0], "cell": {"editorID": CELL}}
        if method == "Nav.plan":
            top = params["z"]
            return {"found": True, "complete": True, "goalOnMesh": True, "startLevelZ": 8085.0, "goalLevelZ": top,
                    "points": [[0, 0, 8085], [100, 1000, 8200], [150, 2500, 8350], [200, 3965, top]], "length": 4100.0}
        if method == "Nav.status":
            return {"active": False, "last": {"state": "arrived", "pos": [200, 3965, WALKWAY[2]], "travelled": 4300.0,
                                              "stalls": {"totalS": 0.0}}}
        return {}


async def standalone(args):
    if args.dry_run:
        ctx = DryCtx()
        result = await run(ctx, 1)
        print(json.dumps({"dryRun": True, "calls": len(ctx.calls), "pass": result["pass"]}), flush=True)
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
        result = await run(ctx, args.passes)
    finally:
        await manager.stop()
    print(json.dumps(result, default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if not options.dry_run and not options.config:
        parser.error("--config is required without --dry-run")
    asyncio.run(standalone(options))
