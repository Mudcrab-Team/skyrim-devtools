#!/usr/bin/env python3.13
"""After a crashed script the host must be idle: no game process and no session lease (SPDX-License-Identifier: GPL-3.0-or-later)."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.smoke import WindowsHost


async def main(config):
    host = WindowsHost(json.loads(Path(config).read_text()))
    state = await host.command("idle")
    print(json.dumps({"pass": bool(state.get("idle")), "details": state}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    asyncio.run(main(parser.parse_args().config))
