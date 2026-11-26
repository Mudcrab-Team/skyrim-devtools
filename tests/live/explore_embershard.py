#!/usr/bin/env python3.13
"""Live check of cell_map and explore in Embershard Mine (SPDX-License-Identifier: GPL-3.0-or-later).

  python3.13 tests/live/explore_embershard.py --config mac.json [--cell EmbershardMine01] [--save Fixture] [--out DIR]

Loads the save, enters the cell, checks that cell_map finds sensible rooms and an exit, then runs explore
goal=clear with fight and invulnerable until the cell is cleared. Prints {"pass": bool, "details": {...}}.
The caller holds the PC lock; this script never takes it.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server  # noqa: E402


async def main(options):
    config = json.loads(Path(options.config).expanduser().read_text())
    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    config["cellMapsDir"] = str(out)
    manager = GameManager(config)
    server = build_server(manager)
    details, checks = {}, {}

    async def tool(name, **args):
        res = await server.call_tool(name, args)
        res = res[0] if isinstance(res, tuple) else res
        return res[0].text

    try:
        await tool("game_start", headless=False, save=options.save)
        await tool("console", command=f"coc {options.cell}", target="none")
        for _ in range(40):
            await asyncio.sleep(2)
            try:
                if (await manager.call("Player.state")).get("cell") and (await manager.call("Cell.navmesh"))["triangles"]:
                    break
            except Exception:
                pass
        nav = await manager.call("Cell.navmesh")
        contents = await manager.call("Cell.contents")
        json.dump(nav, open(out / "navmesh.json", "w"))
        json.dump(contents, open(out / "contents.json", "w"))
        began = time.monotonic()
        summary = json.loads(await tool("cell_map", format="json", plot=True))
        details["cellMapS"] = round(time.monotonic() - began, 2)
        details["rooms"] = summary["rooms"]
        details["hostilesAtStart"] = summary["hostilesAlive"]
        details["exit"] = summary["exit"]
        details["plot"] = summary.get("plot")
        checks["rooms 5..40"] = 5 <= summary["rooms"] <= 40
        checks["exit identified"] = bool(summary["exit"])
        checks["hostiles listed"] = summary["hostilesAlive"] > 0
        began = time.monotonic()
        result = json.loads(await tool("explore", goal="clear", fight=True, invulnerable=True, rooms=80, timeout_s=1200))
        details["clearS"] = round(time.monotonic() - began, 1)
        details["status"] = result["status"]
        details["legs"] = len(result["legs"])
        details["killed"] = result["killed"]
        details["failedLegs"] = [leg for leg in result["legs"] if leg.get("outcome") != "arrived"][:10]
        details["hostilesLeft"] = result["map"].get("hostilesAlive")
        checks["cleared"] = result["status"] == "cleared"
        checks["no hostile left"] = result["map"].get("hostilesAlive") == 0
        details["finalMap"] = await tool("cell_map", plot=True)
    except Exception as error:
        details["error"] = repr(error)
    finally:
        try:
            await tool("game_stop")
        except Exception:
            pass
    details["checks"] = checks
    print(json.dumps({"pass": bool(checks) and all(checks.values()) and "error" not in details, "details": details}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--cell", default="EmbershardMine01")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--out", default=".local/explore-live")
    asyncio.run(main(parser.parse_args()))
