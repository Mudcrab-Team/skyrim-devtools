#!/usr/bin/env python3
"""Launch, drive and close the Windows offline Skyrim fixture from the Mac."""

import argparse
import asyncio
import base64
from contextlib import suppress
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess

from .client import INVENTORY, MAX_MESSAGE, Session, observe
from .logs import classify_outcome


ACTIVE = {"starting", "running"}
MENU_SETTLE = 0.4


@dataclass
class Counts:
    sessions: int = 0
    loaded: int = 0
    inventory_cycles: int = 0
    events: int = 0
    tab_taps: int = 0
    tab_taps_unfocused: int = 0
    tab_failures: int = 0
    console_queries: int = 0
    reloads: int = 0
    clean_stops: int = 0
    headless: bool = False


class WindowsHost:
    def __init__(self, config):
        destination = config["sshDestination"]
        if not isinstance(destination, str) or not destination or destination.startswith("-"):
            raise ValueError("Set an SSH destination or alias in the local configuration")
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
        if path := config.get("sshConfig"):
            self.ssh += ["-F", str(Path(path).expanduser())]
        self.destination = destination
        self.local_port = config.get("localPort", 48681)
        if type(self.local_port) is not int or not 1024 <= self.local_port <= 65535:
            raise ValueError("Local port must be between 1024 and 65535")

    async def command(self, action, session_id="", mode="", grace=None, force=False):
        if action not in {"start", "status", "stop", "evidence", "crashes", "health", "reset"} or (session_id and not re.fullmatch("[a-f0-9]{32}", session_id)):
            raise ValueError("Invalid Windows session operation")
        if mode not in {"", "headless", "windowed"}:
            raise ValueError("Invalid display mode")
        argument = f"-Mode {mode}" if mode else session_id
        if grace is not None:
            if type(grace) is not int or not 1 <= grace <= 120:
                raise ValueError("Grace period must be 1 to 120 seconds")
            argument += f" -Grace {grace}"
        if force:
            argument += " -Force"
        script = rf"& (Join-Path $env:USERPROFILE '.local\skyrim-devtools\host.ps1') {action} {argument}"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        process = await asyncio.create_subprocess_exec(
            *self.ssh, "-T", self.destination, "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "RemoteSigned",
            "-EncodedCommand", encoded, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(45):
                data, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise RuntimeError(f"Windows host {action} failed")
        try:
            return json.loads(data)
        except ValueError as error:
            raise RuntimeError(f"Invalid response from Windows host {action}") from error

    async def wait_ready(self, session_id):
        async def read():
            state = await self.command("status", session_id)
            if state["phase"] not in ACTIVE:
                raise RuntimeError(f"Windows host ended during startup ({state['phase']}, {state['reason']})")
            return state

        return await observe(read, lambda state: state["phase"] == "running", "Windows launch", 60)

    async def stop(self, session_id, grace=12):
        """Ask the game to quit; the host kills it when it has not exited after `grace` seconds."""
        await self.command("stop", session_id, grace=grace)

        async def read():
            return await self.command("status", session_id)

        try:
            state = await observe(read, lambda state: state["phase"] not in ACTIVE, "Windows shutdown", grace + 10)
        except (TimeoutError, asyncio.TimeoutError):
            # the runner is hung too: kill what is left so the next start does not meet a busy task
            await self.reset(session_id, force=True)
            raise
        await self.reset(session_id)
        return state

    async def reset(self, session_id="", force=False):
        """End the scheduled task and drop the lease marker of a finished session, so the next start works.
        Nothing is touched while a game process runs unless force is set."""
        try:
            return await self.command("reset", session_id, force=force)
        except (RuntimeError, TimeoutError, asyncio.TimeoutError, ValueError):
            return {"cleared": [], "error": "reset failed"}

    async def tunnel(self, port):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Windows host returned an invalid port")
        return await asyncio.create_subprocess_exec(
            *self.ssh, "-NT", "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{port}", self.destination,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


async def stop_outcome(host, state, session_id):
    """Classifies a finished run; an abnormal end is paired with the evidence the host saved."""
    evidence = None
    if state.get("evidence"):
        try:
            evidence = await host.command("evidence", session_id)
        except (RuntimeError, TimeoutError, ValueError):
            pass
    return classify_outcome(state, evidence)


async def exercise(session, counts, cycles, session_only=False, tab_trials=20, timings=None, screenshot=None):
    timings = {} if timings is None else timings
    clock = asyncio.get_running_loop().time
    began = clock()
    needed = {"loadSave", "events"} if session_only else {"loadSave", "events", "menus", "input", "console"}
    info = await observe(session.info, lambda state: state["dataLoaded"], "Game data", 120)
    if not needed <= set(info["capabilities"]):
        raise RuntimeError(f"The host lacks required capabilities: {sorted(needed - set(info['capabilities']))}")
    counts.sessions += 1
    counts.headless = bool(info.get("headless"))
    await session.subscribe("menuOpenClose", "loadGame", "cellChange", "console")

    await observe(session.snapshot, lambda state: state["open"].get("Main Menu"), "Main menu", 60)
    timings["mainMenuSeconds"] = round(clock() - began, 1)

    started = clock()
    await session.load_save("Fixture")
    await observe(session.snapshot, lambda state: state["playable"], "Save load", 60)
    timings["loadSeconds"] = round(clock() - started, 1)
    await session.next_event(lambda event: event["type"] == "loadGame" and event["data"]["success"], 5)
    counts.loaded += 1
    if session_only:
        return

    if screenshot:
        await take_screenshots(session, Path(screenshot), timings)

    for _ in range(cycles):
        await session.set_menu(INVENTORY, True)
        await session.set_menu(INVENTORY, False)
        counts.inventory_cycles += 1
    menu_events = [event for event in session.drain_events() if event["type"] == "menuOpenClose"
                   and event["data"]["menu"] == INVENTORY]
    if len(menu_events) < 2 * cycles:
        raise RuntimeError(f"Expected {2 * cycles} inventory events, saw {len(menu_events)}")
    counts.events += len(menu_events)

    async def ready_inventory():
        await session.set_menu(INVENTORY, True)
        await observe(session.snapshot, lambda state: state["open"].get(INVENTORY), "Inventory movie", 8)
        # A loaded movie still ignores input during its opening transition (about 0.2 s).
        await asyncio.sleep(MENU_SETTLE)

    for _ in range(tab_trials):
        await ready_inventory()
        foreground = (await session.snapshot())["foreground"]
        await session.key("Tab")
        try:
            await observe(session.snapshot, lambda state: INVENTORY not in state["open"], "Tab closes inventory", 5)
        except TimeoutError:
            counts.tab_failures += 1
            with suppress(OSError, RuntimeError):
                await session.set_menu(INVENTORY, False)
            continue
        counts.tab_taps += 1
        if not foreground:
            counts.tab_taps_unfocused += 1

    lines = await session.console("GetSKSEVersion")
    if not any("2.3.1" in line for line in lines):
        raise RuntimeError(f"GetSKSEVersion printed {lines!r}")
    counts.console_queries += 1
    lines = await session.console("GetAV Health", target="player")
    if not lines:
        raise RuntimeError("GetAV Health printed nothing")
    counts.console_queries += 1
    timings["consoleSample"] = lines[:2]

    started = clock()
    await session.reload()
    await observe(session.snapshot, lambda state: state["playable"], "Reload", 60)
    timings["reloadSeconds"] = round(clock() - started, 1)
    counts.reloads += 1
    await session.ping()
    if counts.tab_failures:
        raise RuntimeError(f"Tab closed the inventory {counts.tab_taps}/{tab_trials} times")


async def take_screenshots(session, path, timings):
    """Saves the world view to path and the Inventory view next to it, then samples frame times."""
    clock = asyncio.get_running_loop().time
    shots = {}
    started = clock()
    shots["world"] = await session.capture(path)
    shots["world"]["roundTripMs"] = round((clock() - started) * 1000)
    await session.set_menu(INVENTORY, True)
    await observe(session.snapshot, lambda state: state["open"].get(INVENTORY), "Inventory movie", 8)
    await asyncio.sleep(MENU_SETTLE)
    started = clock()
    shots["inventory"] = await session.capture(path.with_name(f"{path.stem}-inventory{path.suffix}"))
    shots["inventory"]["roundTripMs"] = round((clock() - started) * 1000)
    await session.set_menu(INVENTORY, False)
    timings["screenshots"] = shots
    timings["frameStats"] = await session.frame_stats(window=10)


async def connect(connect_ws, local_port, token):
    uri = f"ws://127.0.0.1:{local_port}/rpc"
    deadline = asyncio.get_running_loop().time() + 45
    while True:
        try:
            return await connect_ws(
                uri, additional_headers={"Authorization": f"Bearer {token}"},
                compression=None, proxy=None, open_timeout=2, max_size=MAX_MESSAGE,
            )
        except (OSError, TimeoutError):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Windows plugin endpoint did not start") from None
            await asyncio.sleep(0.3)


async def run(host, connect_ws, counts, cycles, session_only, tab_trials=20, mode="", timings=None, screenshot=None):
    timings = {} if timings is None else timings
    began = asyncio.get_running_loop().time()
    bootstrap = await host.command("start", mode=mode)
    session_id = bootstrap["sessionId"]
    token = bootstrap["token"]
    tunnel = None
    socket = None
    scenario_failure = None
    try:
        if not re.fullmatch("[a-f0-9]{32}", session_id) or not re.fullmatch("[a-f0-9]{64}", token):
            raise RuntimeError("Invalid session bootstrap")
        await host.wait_ready(session_id)
        tunnel = await host.tunnel(bootstrap["port"])
        socket = await connect(connect_ws, host.local_port, token)
        session = Session(socket, session_id)
        await exercise(session, counts, cycles, session_only, tab_trials, timings, screenshot)
        timings["sessionSeconds"] = round(asyncio.get_running_loop().time() - began, 1)
    except Exception as error:
        scenario_failure = error
        raise
    finally:
        try:
            stopped = await host.stop(session_id)
            outcome = await stop_outcome(host, stopped, session_id)
            if outcome.clean:
                counts.clean_stops += 1
            elif outcome.kind != "startup-failure" or counts.loaded:
                raise RuntimeError(f"Windows game did not stop cleanly - {outcome.summary}")
        except Exception as error:
            if scenario_failure is None:
                raise
            raise RuntimeError(f"{scenario_failure}\nShutdown also failed ({error})") from scenario_failure
        finally:
            try:
                if socket is not None:
                    await socket.close()
            finally:
                if tunnel is not None and tunnel.returncode is None:
                    tunnel.terminate()
                    await tunnel.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--tab-trials", type=int, default=20)
    parser.add_argument("--session-only", action="store_true")
    parser.add_argument("--mode", choices=["headless", "windowed"], help="override the host's display mode")
    parser.add_argument("--screenshot", type=Path, metavar="PNG",
                        help="save the world view here and the Inventory view as <name>-inventory.png")
    options = parser.parse_args()
    if not 1 <= options.cycles <= 100:
        parser.error("--cycles must be between 1 and 100")
    if not 1 <= options.tab_trials <= 200:
        parser.error("--tab-trials must be between 1 and 200")

    try:
        from websockets.asyncio.client import connect as connect_ws
    except ImportError:
        parser.error("Install the Python dependencies in requirements.txt")
    from websockets.exceptions import WebSocketException

    counts = Counts()
    timings = {}
    result = 0
    try:
        config = json.loads(options.config.read_text())
        asyncio.run(run(WindowsHost(config), connect_ws, counts, options.cycles, options.session_only,
                        options.tab_trials, options.mode or "", timings, options.screenshot))
    except (OSError, ValueError, RuntimeError, KeyError, WebSocketException) as error:
        print(f"FAIL {error}")
        result = 1
    except KeyboardInterrupt:
        print("FAIL interrupted")
        result = 1
    print(json.dumps(asdict(counts), sort_keys=True))
    print(json.dumps(timings, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
