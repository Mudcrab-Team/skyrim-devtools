#!/usr/bin/env python3.13
"""Live check of cell_map and explore in Embershard Mine (SPDX-License-Identifier: GPL-3.0-or-later).

Run by skyrim-devtools-livetest on the shared game session (setup: the Fixture save). The script enters the
cell with `coc`, checks that cell_map finds sensible rooms, hostiles and an exit, then runs explore goal=clear
with fight and invulnerable until it reports cleared, and checks that no hostile is left.

  python3.13 tests/live/explore_embershard.py --dry-run     # exercises the script against a fake game
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skyrim_devtools.use.server import build_server  # noqa: E402

CELL = "EmbershardMine01"


async def run(ctx, cell=CELL, tool=None):
    manager = ctx.manager
    details, checks = {}, {}
    if tool is None:
        server = build_server(manager)

        async def tool(name, **args):
            res = await server.call_tool(name, args)
            res = res[0] if isinstance(res, tuple) else res
            return res[0].text

    out = Path(ctx.artifacts) if getattr(ctx, "artifacts", None) else Path(".local/explore-live")
    out.mkdir(parents=True, exist_ok=True)
    manager.config["cellMapsDir"] = str(out)
    try:
        await tool("console", command=f"coc {cell}", target="none")
        for _ in range(40):
            await asyncio.sleep(2)
            try:
                if (await manager.call("Cell.navmesh"))["triangles"]:
                    break
            except Exception:
                pass
        began = time.monotonic()
        summary = json.loads(await tool("cell_map", format="json", plot=True))
        details.update(cellMapS=round(time.monotonic() - began, 2), rooms=summary["rooms"], hostilesAtStart=summary["hostilesAlive"],
                       exit=summary["exit"], plot=summary.get("plot"))
        checks["rooms 5..40"] = 5 <= summary["rooms"] <= 40
        checks["exit identified"] = bool(summary["exit"])
        checks["hostiles listed"] = summary["hostilesAlive"] > 0
        began = time.monotonic()
        result = json.loads(await tool("explore", goal="clear", fight=True, invulnerable=True, rooms=80, timeout_s=1200))
        details.update(clearS=round(time.monotonic() - began, 1), status=result["status"], legs=len(result["legs"]),
                       killed=result["killed"], hostilesLeft=result["map"].get("hostilesAlive"),
                       failedLegs=[leg for leg in result["legs"] if leg.get("outcome") != "arrived"][:10])
        details.update(logFile=result.get("logFile"), pulls=result.get("pulls"), logTail=result.get("log", [])[-25:])
        if result["status"] in ("stuck", "cleared_reachable"):
            details["why"] = result.get("why")
            details["hostiles"] = result.get("hostiles") or result.get("unreachable")
        checks["cleared"] = result["status"] == "cleared"
        checks["no hostile left"] = result["map"].get("hostilesAlive") == 0
        checks["step log written"] = bool(result.get("logFile")) and Path(result["logFile"]).exists()
        details["finalMap"] = await tool("cell_map", plot=True)
    except Exception as error:
        details["error"] = repr(error)
    details["checks"] = checks
    return {"pass": bool(checks) and all(checks.values()) and "error" not in details, "details": details}


def dry_run():
    sys.path.insert(0, str(ROOT / "tests"))
    import test_rooms
    import test_use

    class Ctx:
        artifacts = ROOT / ".local" / "explore-dry"

    async def go():
        test_use.METHODS.extend(test_rooms.TOOL_METHODS)
        h = test_use.Harness()
        await h.text("game_start")
        socket = h.sockets[0]
        socket.results["Cell.navmesh"] = test_rooms.two_rooms()
        socket.results["Cell.contents"] = test_rooms.empty_contents(actors=[])
        socket.results["Cell.visited"] = {"points": [[600, 600, 0]]}
        socket.results["Nav.status"] = {"active": False, "last": {"state": "arrived"}}
        ctx = Ctx()
        ctx.manager = h.manager
        result = await run(ctx, tool=lambda name, **a: h.text(name, **a))
        await h.text("game_stop")
        return result

    return asyncio.run(go())


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        print(json.dumps(dry_run()))
    else:
        raise SystemExit("run this script through skyrim-devtools-livetest")
