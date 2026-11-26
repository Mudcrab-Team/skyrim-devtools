"""game_save: save the running game and keep the save across launches.

The host clears the profile's Saves folder on every start and restores only the files in its fixtures
folder, so a plain console `save` is gone after the next game_start. This tool saves and copies the
file into the fixtures folder so a later session can load_save it by name.
"""

import asyncio
import base64
import json
import re
import subprocess

from .manager import GameError

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
RESERVED = {"fixture"}


def keep_script(name, remove):
    root = r"Join-Path $env:USERPROFILE '.local\skyrim-devtools'"
    if remove:
        return (rf"$r = {root}; $f = Join-Path $r 'fixtures\{name}.ess'; "
                rf"if (Test-Path -LiteralPath $f) {{ Remove-Item -LiteralPath $f }}; ConvertTo-Json -Compress @{{kept = $false}}")
    return (rf"$r = {root}; $s = Join-Path $r 'profile\Saves\{name}.ess'; "
            rf"if (-not (Test-Path -LiteralPath $s)) {{ throw 'save file not found' }}; "
            rf"New-Item -ItemType Directory -Force (Join-Path $r 'fixtures') | Out-Null; "
            rf"Copy-Item -LiteralPath $s -Destination (Join-Path $r 'fixtures\{name}.ess') -Force; "
            rf"ConvertTo-Json -Compress @{{kept = $true; bytes = (Get-Item -LiteralPath $s).Length}}")


def register(mcp, manager, native):
    @mcp.tool()
    async def game_save(name: str, keep: bool = True) -> str:
        """Save the game under `name` (letters, digits, - and _) and, with keep=true, keep the save across
        game_stop / game_start: the host wipes its Saves folder on every launch, so the file is also copied to
        the host's fixtures folder. Load it later with game_start {save: name} or load_save. keep=false removes a
        kept copy again (use it to stop saves piling up for other users of the test PC)."""
        if not NAME_RE.fullmatch(name) or name.lower() in RESERVED:
            raise GameError("name is 1-40 letters, digits, - or _, and not the reserved fixture name")
        if not keep:
            return await _run(manager, keep_script(name, True))
        await manager.call("Console.execute", command=f"save {name}", target="player", timeout=20)
        for _ in range(20):
            saves = await manager.call("Session.listSaves")
            if any(item.get("name") == name for item in saves):
                break
            await asyncio.sleep(0.5)
        else:
            raise GameError("the game did not write the save")
        return await _run(manager, keep_script(name, False))


async def _run(manager, script):
    host = manager.host
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process = await asyncio.create_subprocess_exec(
        *host.ssh, "-T", host.destination, "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "RemoteSigned", "-EncodedCommand", encoded,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await process.communicate()
    if process.returncode:
        raise GameError(f"could not keep the save: {err.decode(errors='replace').strip()[:200]}")
    return json.dumps(json.loads(out))
