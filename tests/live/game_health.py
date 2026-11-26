"""Live check of the game-health and recovery paths (SPDX-License-Identifier: GPL-3.0-or-later).

  python3.13 tests/live/game_health.py --config devtools.json [--save Fixture] [--minutes 5] [--interval 30]

Copies windows/host.ps1 and windows/perf-watch.ps1 to the host first.
Cases, one JSON verdict on the last stdout line ({"pass": bool, "details": {...}}):
  health     start the game (visible), idle with light activity for --minutes, read game_status health every
             --interval seconds: thread count present and plausible, process memory and uptime present, the
             window answering; then a lowered threshold must raise the threadCount warning.
  restart    game_stop and at once game_start again must succeed, with the host left clean in between.
  stale      a leftover lease marker (old, no game running) is cleared by game_start itself.
  hold       pc-build hold with a short limit stops its command, exits 124 and leaves its lock file free.
  perf-watch the sampler runs detached at about 1 Hz and records the game's thread count.
"""

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server, load_config  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402

REMOTE = r"$env:USERPROFILE\.local\skyrim-devtools"


class Rig:
    def __init__(self, config):
        self.config = config
        self.manager = GameManager(config)
        self.server = build_server(self.manager, Recorder())

    async def tool(self, name, **args):
        out = await self.server.call_tool(name, args)
        out = out[0] if isinstance(out, tuple) else out
        return out[0].text if hasattr(out[0], "text") else str(out[0])

    async def json(self, name, **args):
        return json.loads(await self.tool(name, **args))

    async def powershell(self, script):
        host = self.manager.host
        process = await asyncio.create_subprocess_exec(
            # the host's login shell is PowerShell, which would expand $variables in a plain -Command argument
            *host.ssh, "-T", host.destination, "powershell", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand",
            base64.b64encode(("$ProgressPreference = 'SilentlyContinue'; " + script).encode("utf-16-le")).decode(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        data, _ = await process.communicate()
        return data.decode(errors="replace").strip()


def deploy_scripts(rig, details):
    """Copy this checkout's host scripts to the host's scratch folder; the cases below test them, not older copies."""
    host = rig.manager.host
    scp = ["scp", "-q", *host.ssh[1:]]
    for name in ("host.ps1", "perf-watch.ps1"):
        done = subprocess.run([*scp, str(ROOT / "windows" / name), f"{host.destination}:.local/skyrim-devtools/{name}"],
                              capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"could not copy {name}: {done.stderr.strip()[:200]}")
    details["scriptsDeployed"] = True


async def case_health(rig, save, minutes, interval, details):
    started = await rig.json("game_start", headless=False, save=save)
    details["start"] = {k: started.get(k) for k in ("phase", "mode", "staleCleared")}
    await rig.tool("wait_for", timeout_s=60, playable=True)
    readings = []
    deadline = time.monotonic() + minutes * 60
    while True:
        status = await rig.json("game_status")
        health = status.get("health") or {}
        readings.append({"t": round(time.monotonic() - (deadline - minutes * 60)), **{
            k: health.get(k) for k in ("threads", "handles", "privateMB", "workingSetMB", "cpuCores", "responding", "uptimeS")}})
        print("health", json.dumps(readings[-1]), flush=True)
        await rig.tool("snapshot")
        if time.monotonic() + interval > deadline:
            break
        await asyncio.sleep(interval)
    details["readings"] = readings
    threads = [r["threads"] for r in readings]
    ok = bool(readings) and all(isinstance(t, int) and 20 <= t <= 20000 for t in threads)
    ok = ok and all(isinstance(r["privateMB"], (int, float)) and r["privateMB"] > 100 for r in readings)
    ok = ok and all(r["responding"] is True for r in readings)
    uptimes = [r["uptimeS"] for r in readings]
    ok = ok and uptimes == sorted(uptimes) and len(set(uptimes)) == len(uptimes)
    details["threadsFirstLast"] = [threads[0], threads[-1]] if threads else None
    # the warning path end to end: a limit just under the live count must produce the warning
    rig.manager.health_watch.config["healthThreadWarn"] = max(1, threads[-1] - 5)
    status = await rig.json("game_status")
    codes = [w["code"] for w in status.get("healthWarnings", [])]
    details["thresholdWarning"] = codes
    return ok and "threadCount" in codes


async def case_restart(rig, save, details):
    began = time.monotonic()
    stopped = await rig.json("game_stop")
    details["stop"] = {"path": stopped.get("path"), "cleanExit": stopped.get("cleanExit"),
                       "s": round(time.monotonic() - began, 1)}
    leftover = await rig.powershell(
        "$r = Join-Path $env:USERPROFILE '.local\\skyrim-devtools'; 'lease=' + (Test-Path (Join-Path $r 'active')); "
        "'task=' + (Get-ScheduledTask -TaskName SkyrimDevTools).State; "
        "'game=' + [bool](Get-Process SkyrimSE -ErrorAction SilentlyContinue)")
    details["hostAfterStop"] = leftover.replace("\r", "").split("\n")
    clean = "lease=False" in leftover and "task=Ready" in leftover and "game=False" in leftover
    began = time.monotonic()
    started = await rig.json("game_start", headless=False, save=save)
    details["restart"] = {"phase": started.get("phase"), "s": round(time.monotonic() - began, 1),
                          "staleCleared": started.get("staleCleared")}
    return clean and started.get("phase") == "running"


async def case_stale(rig, save, details):
    await rig.json("game_stop")
    made = await rig.powershell(
        "$r = Join-Path $env:USERPROFILE '.local\\skyrim-devtools'; $f = Join-Path $r 'active'; Set-Content $f ('c' * 32); "
        "(Get-Item $f).LastWriteTime = (Get-Date).AddMinutes(-10); Test-Path $f")
    details["staleLeaseWritten"] = made
    started = await rig.json("game_start", headless=False, save=save)
    details["start"] = {"phase": started.get("phase"), "staleCleared": started.get("staleCleared")}
    return started.get("phase") == "running" and "lease-removed" in (started.get("staleCleared") or [])


def case_hold(config_path, details):
    tool = str(ROOT / "tools" / "pc-build")
    base = [sys.executable, tool, "--config", config_path, "--name", "gh-live", "--lock-file", "gh-live.lock"]
    began = time.monotonic()
    held = subprocess.run([*base, "hold", "--lock-wait", "60", "--max-minutes", "0.1", "--", "sleep", "120"],
                          capture_output=True, text=True, timeout=240)
    details["hold"] = {"exit": held.returncode, "s": round(time.monotonic() - began, 1), "stderr": held.stderr.strip()[-200:]}
    # the probe lock must be free right after: taking it again must work at once
    taken = subprocess.run([*base, "lock"], capture_output=True, text=True, timeout=60)
    subprocess.run([*base, "unlock"], capture_output=True, text=True, timeout=60)
    details["lockFreeAfter"] = taken.returncode == 0
    return held.returncode == 124 and taken.returncode == 0


async def case_perf_watch(rig, details):
    script = rf"{REMOTE}\perf-watch.ps1"
    out = rf"{REMOTE}\gh-live-perf.csv"
    process = await rig.powershell(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Out \"{out}\" -Detach")
    await asyncio.sleep(25)
    rows = await rig.powershell(f"(Get-Content \"{out}\").Count; (Get-Content \"{out}\" -Tail 1).Split(',')[15]")
    await rig.powershell(f"powershell -NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Out \"{out}\" -Stop; "
                         f"Remove-Item \"{out}*\" -ErrorAction SilentlyContinue")
    details["perfWatch"] = {"detach": process, "output": rows.split()}
    try:
        count, threads = rows.split()[:2]
        return int(count) >= 18 and int(threads) > 0
    except ValueError:
        return False


async def main_async(args):
    config = load_config(args.config)
    rig = Rig(config)
    details, verdicts = {}, {}
    try:
        deploy_scripts(rig, details)
        for name, case in (("health", lambda: case_health(rig, args.save, args.minutes, args.interval, details)),
                           ("restart", lambda: case_restart(rig, args.save, details)),
                           ("stale", lambda: case_stale(rig, args.save, details))):
            try:
                verdicts[name] = bool(await case())
            except Exception as error:
                verdicts[name] = False
                details[f"{name}Error"] = f"{type(error).__name__}: {error}"
            print(name, verdicts[name], flush=True)
        try:
            verdicts["perfWatch"] = await case_perf_watch(rig, details)
        except Exception as error:
            verdicts["perfWatch"] = False
            details["perfWatchError"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            await rig.tool("game_stop")
        except Exception:
            pass
    try:
        verdicts["hold"] = case_hold(str(args.config), details)
    except Exception as error:
        verdicts["hold"] = False
        details["holdError"] = f"{type(error).__name__}: {error}"
    details["cases"] = verdicts
    return {"pass": all(verdicts.values()), "details": details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--minutes", type=float, default=5)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    result = asyncio.run(main_async(args))
    print(json.dumps(result), flush=True)
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
