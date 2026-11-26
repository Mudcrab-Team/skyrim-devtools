#!/usr/bin/env python3.13
"""Where do the game's growing threads come from (SPDX-License-Identifier: GPL-3.0-or-later).

  tests/live/thread_leak.py --config mac.json --save NAME --label full-mover [--minutes 10] [--interval 60]
      [--mover | --idle] [--modules Events,Session,...] [--max-growth N] [--out DIR]

One visible game session. Every --interval seconds the host-side windows/thread-census.ps1 groups the game's
threads by start address, state and wait reason; a full stack census runs at the start and at the end so growth can
be attributed to the call sites that own the threads (module+offset only). With --modules the plugin installs just
that allow-list (host config key `modules`, restored afterwards), which separates what the plugin creates from what
the game, SKSE or the driver creates. Per-sample census lines are written to DIR/<label>.jsonl; game-derived names
stay in that local file, the final JSON line only carries counts and module+offset.

Pass: the run completed with samples; with --max-growth, thread growth per minute must also stay below it.
"""
import argparse
import asyncio
import base64
import json
import math
import random
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skyrim_devtools.use.manager import GameManager  # noqa: E402

CENSUS = REPO / "windows" / "thread-census.ps1"
REMOTE_DIR = ".local\\skyrim-devtools"
REMOTE_CONFIG = f"$env:USERPROFILE\\{REMOTE_DIR}\\config.json"
REMOTE_CENSUS = f"$env:USERPROFILE\\{REMOTE_DIR}\\thread-census.ps1"
# What the harness needs to load a save and walk: everything else is left out of the minimal variant.
MINIMAL_MODULES = "Events,Session,Input,NavMove,StatePlayer"
NEWEST = 6


def slope_per_min(points):
    """Least-squares slope of (minutes, value) points; 0.0 with fewer than two distinct times."""
    if len(points) < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / spread if spread else 0.0


def group_counts(census):
    """{(start, state, waitReason): count} of one census."""
    return {(g["start"], g["state"], g["waitReason"]): g["count"] for g in census.get("groups", [])}


def signature_counts(census):
    """Counter of owner call-site signatures from an all-stacks census."""
    counts = Counter()
    for group in census.get("groups", []):
        for signature in group.get("signatures", []):
            counts[signature["frames"] or f"(no own frames) {group['start']}"] += signature["count"]
    return counts


def growth_by_group(samples, top=8):
    """Groups ordered by thread-count slope per minute over the run. samples: [(seconds, census)]."""
    series = {}
    for seconds, census in samples:
        counts = group_counts(census)
        for key in set(series) | set(counts):
            series.setdefault(key, []).append((seconds / 60, counts.get(key, 0)))
    rows = []
    for key, points in series.items():
        # a group that appeared late is padded with zeros up to its first sample time
        rate = slope_per_min(points)
        rows.append({"start": key[0], "state": key[1], "waitReason": key[2], "perMin": round(rate, 2),
                     "first": points[0][1], "last": points[-1][1]})
    rows.sort(key=lambda r: -r["perMin"])
    return [r for r in rows[:top] if r["perMin"] > 0.05]


def signature_growth(first, last, top=8):
    before, after = signature_counts(first), signature_counts(last)
    rows = [{"frames": key, "first": before.get(key, 0), "last": after.get(key, 0), "delta": after.get(key, 0) - before.get(key, 0)}
            for key in set(before) | set(after)]
    rows.sort(key=lambda r: -r["delta"])
    return [r for r in rows[:top] if r["delta"] > 0]


def evaluate(samples, max_growth=None):
    """samples: [(seconds, census)] with alive censuses. Returns (pass, checks)."""
    alive = [(t, c) for t, c in samples if c.get("alive")]
    points = [(t / 60, c["total"]) for t, c in alive]
    growth = slope_per_min(points)
    checks = {"samples": len(samples), "aliveSamples": len(alive),
              "threadsFirst": points[0][1] if points else None, "threadsLast": points[-1][1] if points else None,
              "growthPerMin": round(growth, 2)}
    ok = len(alive) >= 2
    if max_growth is not None:
        ok = ok and growth < max_growth
    return ok, checks


def out(**fields):
    print(json.dumps(fields, default=str), flush=True)


async def powershell(manager, script, timeout=60):
    host = manager.host
    process = await asyncio.create_subprocess_exec(
        # the host's login shell is PowerShell and would expand $variables in a plain -Command argument
        *host.ssh, "-T", host.destination, "powershell", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand",
        base64.b64encode(("$ProgressPreference = 'SilentlyContinue'; " + script).encode("utf-16-le")).decode(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        data, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return ""
    return data.decode(errors="replace").strip()


async def upload_census(manager):
    host = manager.host
    scp = [part for part in host.ssh if part != "ssh"]
    process = await asyncio.create_subprocess_exec(
        "scp", "-q", *scp, str(CENSUS), f"{host.destination}:{REMOTE_DIR.replace(chr(92), '/')}/thread-census.ps1",
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = await process.communicate()
    if process.returncode:
        raise RuntimeError(f"could not upload the census script: {err.decode(errors='replace')[:200]}")


async def census(manager, all_stacks=False):
    flags = f"-NewestStacks {NEWEST}" + (" -AllStacks" if all_stacks else "")
    raw = await powershell(manager, f"& \"{REMOTE_CENSUS}\" {flags}", timeout=180 if all_stacks else 60)
    try:
        return json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError):
        return {"alive": None, "error": raw[:200]}


async def set_modules(manager, modules):
    """Edit the host config key read at game start; None removes it."""
    if modules:
        script = (f"$f = \"{REMOTE_CONFIG}\"; $c = Get-Content $f -Raw | ConvertFrom-Json; "
                  f"$c | Add-Member -NotePropertyName modules -NotePropertyValue '{modules}' -Force; "
                  "$c | ConvertTo-Json | Set-Content $f")
    else:
        script = (f"$f = \"{REMOTE_CONFIG}\"; $c = Get-Content $f -Raw | ConvertFrom-Json; "
                  "$c.PSObject.Properties.Remove('modules'); $c | ConvertTo-Json | Set-Content $f")
    await powershell(manager, script)


async def attempt(manager, method, **params):
    try:
        return await manager.call(method, **params)
    except Exception as error:  # a stalled or dying game must not stop the sampling
        return {"error": f"{type(error).__name__}: {str(error)[:120]}"}


class Mover:
    """Random-heading walks with a periodic return to the start, logging net displacement per cycle so a
    character pressed against a wall is visible in the result."""

    def __init__(self, manager, start, seed=7):
        self.manager, self.start, self.random = manager, start, random.Random(seed)
        self.cycles, self.errors, self.stop = 0, 0, False
        self.displacement = []

    async def position(self):
        state = await attempt(self.manager, "Player.state")
        pos = state.get("pos") if isinstance(state, dict) else None
        return pos if isinstance(pos, (list, tuple)) else None

    async def run(self):
        while not self.stop:
            before = await self.position()
            heading = self.random.uniform(0, 360)
            for method, params in (("Move.turnTo", {"headingDeg": heading, "smooth": True}),
                                   ("Move.walk", {"direction": "forward", "durationMs": 12000, "run": False})):
                self.errors += "error" in await attempt(self.manager, method, **params)
            after = await self.position()
            if before and after:
                self.displacement.append(round(math.dist(before[:2], after[:2])))
            await asyncio.sleep(self.random.uniform(5, 15))
            self.cycles += 1
            if self.cycles % 4 == 0:
                s = self.start
                self.errors += "error" in await attempt(self.manager, "Player.teleport", x=s["x"], y=s["y"], z=s["z"], yawDeg=s["yaw"])
                await asyncio.sleep(3)


async def run_variant(args):
    manager = GameManager(json.loads(args.config.read_text()))
    args.out.mkdir(parents=True, exist_ok=True)
    log = (args.out / f"{args.label}.jsonl").open("w")
    samples, details, mover, mover_task = [], {"label": args.label, "minutes": args.minutes, "modules": args.modules or "all",
                                              "mover": args.mover}, None, None
    first_full = last_full = None
    try:
        await upload_census(manager)
        await set_modules(manager, args.modules)
        await manager.start(headless=False, save=args.save)
        state = await manager.call("Player.state")
        pos, rot = state["pos"], state.get("rotation") or [0, 0, 0]
        details["cell"] = (state.get("cell") or {}).get("editorID")
        if args.mover:
            mover = Mover(manager, {"x": pos[0], "y": pos[1], "z": pos[2], "yaw": rot[2]})
            mover_task = asyncio.create_task(mover.run())
        # let loading threads finish so the baseline is the steady state
        await asyncio.sleep(args.settle)
        began = time.monotonic()
        step = 0
        while True:
            at = time.monotonic() - began
            final = at + args.interval > args.minutes * 60
            full = step == 0 or final
            sample = await census(manager, all_stacks=full)
            sample["t"] = round(at)
            samples.append((at, sample))
            log.write(json.dumps(sample) + "\n")
            log.flush()
            out(t=round(at), threads=sample.get("total"), handles=sample.get("handles"), alive=sample.get("alive"))
            if full and sample.get("alive"):
                first_full = first_full or sample
                last_full = sample
            if sample.get("alive") is False or final:
                break
            step += 1
            await asyncio.sleep(max(1.0, args.interval - (time.monotonic() - began - at)))
        if mover:
            details.update(moverCycles=mover.cycles, moverErrors=mover.errors, netDisplacementPerCycle=mover.displacement)
    except Exception as error:
        details["error"] = f"{type(error).__name__}: {error}"
    finally:
        if mover:
            mover.stop = True
        if mover_task:
            mover_task.cancel()
        try:
            await set_modules(manager, None)
            if not args.keep:
                await manager.stop()
        except Exception as error:
            details["cleanupError"] = f"{type(error).__name__}: {error}"
        log.close()
    ok, checks = evaluate(samples, args.max_growth)
    details.update(checks)
    details["growingGroups"] = growth_by_group([(t, c) for t, c in samples if c.get("alive")])
    if first_full and last_full and first_full is not last_full:
        details["growingOwnerSignatures"] = signature_growth(first_full, last_full)
    if last_full:
        details["newestThreadFrames"] = [t.get("frames", [])[:8] for t in last_full.get("newest", [])]
    details["series"] = [{"t": c.get("t"), "threads": c.get("total"), "handles": c.get("handles")} for _, c in samples]
    details["censusFile"] = str(args.out / f"{args.label}.jsonl")
    return {"pass": ok and "error" not in details, "details": details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--save", default=None)
    parser.add_argument("--label", required=True)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--settle", type=float, default=60)
    parser.add_argument("--mover", dest="mover", action="store_true", default=True)
    parser.add_argument("--idle", dest="mover", action="store_false")
    parser.add_argument("--modules", default=None, help="comma-separated plugin module allow-list; 'minimal' for the smallest set")
    parser.add_argument("--max-growth", type=float, default=None)
    parser.add_argument("--out", type=Path, default=REPO / ".local" / "testq" / "artifacts" / "thread-leak")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.modules == "minimal":
        args.modules = MINIMAL_MODULES
    verdict = asyncio.run(run_variant(args))
    print(json.dumps(verdict, default=str), flush=True)
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
