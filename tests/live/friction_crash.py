#!/usr/bin/env python3.13
"""A script that starts its own game and then crashes; the harness must leave no game behind (SPDX-License-Identifier: GPL-3.0-or-later)."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager


async def main(config):
    manager = GameManager(json.loads(Path(config).read_text()))
    await manager.start(headless=False)
    print(json.dumps({"pass": True, "details": {"started": True, "note": "the script raises right after this line"}}), flush=True)
    raise RuntimeError("deliberate crash with the game running")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    asyncio.run(main(parser.parse_args().config))
