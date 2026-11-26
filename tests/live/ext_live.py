"""Live case: extension API, argument warnings and robustness options (SPDX-License-Identifier: MIT).

Needs the example plugin (examples/hello-ext, deployed with `pc-build --deploy --with-example`) next to the main
plugin. Runs on the harness's shared session (`run(ctx)`) or on its own game from the command line:

    python3.13 tests/live/ext_live.py --config devtools.json [--save Fixture]
    python3.13 tests/live/ext_live.py --dry-run     # no game: exercises the script against a fake

Cases (details.cases): extTool, warnings, alwaysActive, cleanTransition, autoDismiss. The risky one (a menu opened
without its game context) runs last.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server, load_config  # noqa: E402
from skyrim_devtools.use.timeline import Recorder  # noqa: E402

BOUNCE_CELL = "QASmoke"
TARGET_CELL = "WhiterunBanneredMare"


class Rig:
    def __init__(self, manager, scale=1.0):
        self.manager = manager
        self.server = build_server(manager, Recorder())
        self.scale = scale
        self.cases = {}
        self.details = {}
        self.errors = []

    async def tool(self, name, /, **args):
        out = await self.server.call_tool(name, args)
        out = out[0] if isinstance(out, tuple) else out
        return "\n".join(part.text if hasattr(part, "text") else str(part) for part in out)

    async def json(self, name, /, **args):
        text = await self.tool(name, **args)
        value, _ = json.JSONDecoder().raw_decode(text.lstrip())
        return value

    async def case(self, name, body):
        try:
            ok, details = await body()
        except Exception as error:
            ok, details = False, {"error": f"{type(error).__name__}: {str(error)[:300]}"}
            self.errors.append(f"{name}: {details['error']}")
        self.cases[name] = bool(ok)
        self.details[name] = details
        print("CASE", name, ok, json.dumps(details)[:300], flush=True)

    def events(self, kind, since):
        items, _ = self.manager.events_since(since)
        return [e for e in items if e["type"] == kind]


async def ext_tool(rig):
    await rig.tool("game_status")  # re-reads the method list and publishes the extension tools
    tools = {t.name: t for t in await rig.server.list_tools()}
    echo = tools.get("hello_ext_echo")
    if echo is None:
        return False, {"error": "hello_ext_echo is not among the tools; is the example plugin deployed?",
                       "tools": sorted(n for n in tools if "hello" in n)}
    reply = await rig.json("hello_ext_echo", text="hi")
    name = None
    try:
        name = (await rig.json("hello_ext_player_name")).get("name")
    except Exception as error:
        name = f"error: {str(error)[:120]}"
    since = rig.manager.event_total
    ping = await rig.json("hello_ext_emit_ping")
    await asyncio.sleep(1.0 * rig.scale)
    events = rig.events("HelloExt.ping", since)
    listed = [m for m in await rig.json("rpc_methods") if m["name"].startswith("HelloExt.")]
    ok = ("hi" in str(reply.get("received")) and bool(name) and not str(name).startswith("error")
          and bool(events) and events[-1]["data"].get("count") == ping.get("count") and len(listed) == 3
          and "text" in (echo.inputSchema.get("required") or []))
    return ok, {"echo": reply, "playerName": name, "ping": ping, "pingEvents": len(events),
                "schemaRequired": echo.inputSchema.get("required"), "registered": [m["name"] for m in listed]}


async def warnings_case(rig):
    reply = await rig.json("hello_ext_echo", text="hi", txt="typo")
    warning = (reply.get("warnings") or [{}])[0]
    await rig.tool("game_options", changes={"strictArgs": True})
    try:
        try:
            await rig.tool("hello_ext_echo", text="hi", txt="typo")
            strict_error = None
        except Exception as error:
            strict_error = str(error)
    finally:
        await rig.tool("game_options", changes={"strictArgs": False})
    ok = (warning.get("unknown") == ["txt"] and warning.get("accepted") == ["text", "times"]
          and bool(strict_error) and "accepted keys: text, times" in strict_error)
    return ok, {"warning": warning, "strictError": strict_error}


async def always_active(rig):
    async def hour_gain(seconds):
        await rig.manager.call("Game.setTimescale", value=4000)
        try:
            before = (await rig.manager.call("Game.time"))["hour"]
            await asyncio.sleep(seconds * rig.scale)
            after = (await rig.manager.call("Game.time"))["hour"]
        finally:
            await rig.manager.call("Game.setTimescale", value=20)
        return round((after - before) % 24, 2)

    out = {}
    await rig.manager.call("Window.place", mode="offscreen")  # hands the foreground back to the desktop
    try:
        await rig.tool("game_options", changes={"alwaysActive": False})
        await asyncio.sleep(2 * rig.scale)
        out["baselineHourGain"] = await hour_gain(4)
        applied = (await rig.json("game_options", changes={"alwaysActive": True}))["native"]
        await asyncio.sleep(2 * rig.scale)
        out["foreground"] = (await rig.manager.call("Options.get"))["foreground"]
        out["activeHourGain"] = await hour_gain(4)
        out["deactivationsBlocked"] = (await rig.manager.call("Options.get"))["deactivationsBlocked"]
        out["applied"] = applied.get("alwaysActive")
    finally:
        await rig.tool("game_options", changes={"alwaysActive": False})
        await rig.manager.call("Window.place", mode="restore")
    # 4 s at timescale 4000 is about 4.4 game hours when the game runs; unfocused-and-paused would give none
    ok = out.get("foreground") is False and out.get("activeHourGain", 0) > 1.0 and out.get("applied") is True
    return ok, out


async def clean_transition(rig):
    out = {}
    await rig.tool("game_options", changes={"cleanTransition": True, "cleanTransitionCell": BOUNCE_CELL})
    try:
        since = rig.manager.event_total
        began = time.monotonic()
        reply = await rig.json("center_on_cell", cell=TARGET_CELL)
        out["seconds"] = round(time.monotonic() - began, 1)
        out["bounced"] = reply.get("bounced")
        await asyncio.sleep(2 * rig.scale)
        entered = [e["data"].get("editorId") for e in rig.events("cellChange", since) if e["data"].get("entered")]
        loading = [e for e in rig.events("menuOpenClose", since) if e["data"]["menu"] == "Loading Menu" and e["data"]["opening"]]
        out["cellsEntered"] = entered
        out["loadingScreens"] = len(loading)
        out["cell"] = ((await rig.manager.call("Player.state")).get("location") or {}).get("cell")
        # the console path must bounce too
        since = rig.manager.event_total
        again = await rig.json("console", command="coc " + BOUNCE_CELL)
        await asyncio.sleep(3 * rig.scale)
        second = await rig.json("console", command="coc " + TARGET_CELL)
        out["consoleBounced"] = second.get("bounced")
        out["consoleCells"] = [e["data"].get("editorId") for e in rig.events("cellChange", since) if e["data"].get("entered")]
        del again
    finally:
        await rig.tool("game_options", changes={"cleanTransition": False})
    ok = (out.get("bounced") is True and BOUNCE_CELL in out.get("cellsEntered", []) and out.get("loadingScreens", 0) >= 2
          and TARGET_CELL.lower() in str(out.get("cell")).lower() and out.get("consoleBounced") is True)
    return ok, out


async def auto_dismiss(rig):
    out = {}
    for menu in ("TutorialMenu", "Journal Menu"):
        applied = await rig.json("game_options", changes={"autoDismissMenus": [menu, "MessageBoxMenu"]})
        out["ignored"] = (applied.get("native") or {}).get("ignored")
        since = rig.manager.event_total
        try:
            await rig.tool("open_menu", name=menu)
        except Exception as error:
            out[menu] = f"open failed: {str(error)[:120]}"
            continue
        deadline = time.monotonic() + 5 * rig.scale
        closed = False
        while time.monotonic() < deadline:
            state = await rig.manager.call("Events.snapshot")
            if menu not in [m["name"] for m in state["menus"]] and rig.events("menuAutoDismissed", since):
                closed = True
                break
            await asyncio.sleep(0.2)
        out[menu] = {"dismissed": closed, "events": len(rig.events("menuAutoDismissed", since))}
        if closed:
            break
    await rig.tool("game_options", changes={"autoDismissMenus": []})
    ok = any(isinstance(v, dict) and v.get("dismissed") for v in out.values()) and out.get("ignored") == ["MessageBoxMenu"]
    return ok, out


async def run_with(manager, scale=1.0):
    rig = Rig(manager, scale)
    await rig.case("extTool", lambda: ext_tool(rig))
    await rig.case("warnings", lambda: warnings_case(rig))
    await rig.case("alwaysActive", lambda: always_active(rig))
    await rig.case("cleanTransition", lambda: clean_transition(rig))
    await rig.case("autoDismiss", lambda: auto_dismiss(rig))
    return {"pass": all(rig.cases.values()) and len(rig.cases) == 5, "details": {"cases": rig.cases, **rig.details, "errors": rig.errors}}


async def run(ctx):
    await ctx.setup("Fixture")
    return await run_with(ctx.manager)


async def standalone(config, save):
    manager = GameManager(config)
    try:
        await manager.start(headless=False, save=save)
        await asyncio.sleep(5)
        return await run_with(manager)
    finally:
        try:
            await manager.stop()
        except Exception as error:
            print("stop failed:", error, flush=True)


async def dry():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_use import FakeSocket, Harness

    FakeSocket.default_extra = [{"name": "HelloExt.echo", "thread": "any", "timeoutMs": 5000, "owner": "HelloExt",
                                 "description": "Echo.", "schema": {"type": "object", "properties": {
                                     "text": {"type": "string"}, "times": {"type": "integer"}}, "required": ["text"],
                                     "additionalProperties": False}}]
    harness = Harness()
    await harness.manager.start(headless=True, save="Fixture")
    # a fake has no game to bounce through or time to advance: the run proves the script and its tool plumbing
    return await run_with(harness.manager, scale=0.02)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if options.dry_run:
        result = asyncio.run(dry())
    else:
        config = options.config or os.environ.get("SKYRIM_LIVETEST_CONFIG")
        result = asyncio.run(standalone(load_config(config), options.save))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
