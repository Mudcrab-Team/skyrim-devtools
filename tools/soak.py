#!/usr/bin/env python3.13
"""Start, load a save, act, stop, repeated; counts clean exits (SPDX-License-Identifier: GPL-3.0-or-later).

  tools/soak.py --config mac.json --cycles 10 [--scenario console,idle,menus,capture,inventory] [--name AGENT]

Run it while holding agent.lock (tools/pc-build lock). With --name it refreshes the lock before every
cycle and aborts when the lock was lost, so a long soak never looks stale to the queue.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from skyrim_devtools.use.manager import GameManager  # noqa: E402

MENUS = ("Journal Menu", "MapMenu", "MagicMenu", "StatsMenu", "InventoryMenu", "TweenMenu")


async def attempt(manager, method, **params):
    try:
        return await manager.call(method, **params)
    except Exception as error:
        return {"error": str(error)[:120]}


async def act(manager, scenario):
    if scenario == "console":
        for command in ("player.additem f 5", "tgm", "tgm"):
            await attempt(manager, "Console.execute", command=command)
            await asyncio.sleep(0.5)
    elif scenario == "menus":
        for name in MENUS:
            await attempt(manager, "UI.openMenu", name=name, wait=True)
            await asyncio.sleep(0.8)
            await attempt(manager, "UI.closeMenu", name=name, wait=True)
            await asyncio.sleep(0.5)
    elif scenario == "inventory":
        await attempt(manager, "UI.openMenu", name="InventoryMenu", wait=True)
        await asyncio.sleep(1.5)
    elif scenario == "capture":
        for _ in range(3):
            await attempt(manager, "Screen.capture", maxWidth=640)
            await asyncio.sleep(0.5)
    else:
        await asyncio.sleep(3)


async def cycle(config, scenario, save):
    manager = GameManager(config)
    record = {"scenario": scenario}
    began = time.monotonic()
    try:
        started = await manager.start(headless=True, save=save)
        record["startRetries"] = len(started.get("startRetries", []))
        record["startSeconds"] = round(time.monotonic() - began)
        await act(manager, scenario)
        stopped = await manager.stop()
        record.update(path=stopped.get("path"), clean=bool(stopped.get("cleanExit")))
        if stopped.get("crash"):
            record["crash"] = stopped["crash"].get("log")
    except Exception as error:
        record.update(error=f"{type(error).__name__}: {error}"[:300], clean=False)
        status = await manager.status()
        record["crash"] = status.get("crash")
        try:
            await manager.stop()
        except Exception:
            pass
    return record


def refresh_lock(config_path, name):
    result = subprocess.run([str(REPO / "tools" / "pc-build"), "--config", str(config_path), "--name", name, "lock", "--refresh"],
                            capture_output=True, text=True)
    return result.returncode == 0


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--scenario", default="console,idle,menus,capture")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--name", help="lock holder name: refresh agent.lock every cycle")
    options = parser.parse_args()
    config = json.loads(options.config.expanduser().read_text())
    scenarios = options.scenario.split(",")
    clean = 0
    for index in range(options.cycles):
        if options.name and not refresh_lock(options.config.expanduser(), options.name):
            print("LOCK LOST: agent.lock is no longer held by", options.name, flush=True)
            break
        record = await cycle(config, scenarios[index % len(scenarios)], options.save)
        clean += bool(record["clean"])
        print(("OK  " if record["clean"] else "BAD ") + json.dumps(record, default=str), flush=True)
    print(f"RESULT {clean}/{options.cycles} clean")
    sys.exit(0 if clean == options.cycles else 1)


if __name__ == "__main__":
    asyncio.run(main())
