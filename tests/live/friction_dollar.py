#!/usr/bin/env python3.13
"""A command containing $variables reaches the host unexpanded by its login shell (SPDX-License-Identifier: GPL-3.0-or-later)."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.smoke import WindowsHost, powershell_argv

# the single-quoted text must come back verbatim; the double-quoted $env expansion must happen on the host only
SCRIPT = "$x = 'lit$eral'; Write-Output $x; Write-Output ($env:USERPROFILE.Length -gt 3)"


async def main(config):
    host = WindowsHost(json.loads(Path(config).read_text()))
    process = await asyncio.create_subprocess_exec(
        *host.ssh, "-T", host.destination, *powershell_argv(SCRIPT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await process.communicate()
    lines = out.decode().split()
    print(json.dumps({"pass": lines == ["lit$eral", "True"], "details": {"lines": lines, "code": process.returncode}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    asyncio.run(main(parser.parse_args().config))
