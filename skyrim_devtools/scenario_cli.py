"""skyrim-devtools-scenario: start the game, apply a scenario file and report what was set up.

  skyrim-devtools-scenario run scenarios/embershard.yaml --config devtools.json [--save Fixture] [--headless] [--keep-running]
  skyrim-devtools-scenario check scenarios/embershard.yaml      validate and print the plan without a game
"""

import argparse
import asyncio
import json
import sys

from .use.manager import GameManager, GameError
from .use.scenario import load_scenario, plan, run_scenario, validate, wait_playable


async def run(options):
    spec = load_scenario(options.file)
    validate(spec)
    config = json.loads(open(options.config or _config_path()).read())
    manager = GameManager(config)
    await manager.start(headless=options.headless, save=options.save or spec.get("save") or "Fixture")
    try:
        await wait_playable(manager.call)
        report = await run_scenario(manager.call, spec)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if options.keep_running:
            print("game left running; stop it with game_stop or the host's stop command", file=sys.stderr)
        return 0 if report["ok"] else 1
    finally:
        if not options.keep_running:
            await manager.stop()


def _config_path():
    import os

    path = os.environ.get("SKYRIM_USE_CONFIG") or os.environ.get("SKYRIM_DEVTOOLS_CONFIG")
    if not path:
        raise SystemExit("pass --config or set SKYRIM_USE_CONFIG")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skyrim-devtools-scenario", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="start the game and apply a scenario file")
    runner.add_argument("file")
    runner.add_argument("--config")
    runner.add_argument("--save", help="save to load first (default: the scenario's save, else Fixture)")
    runner.add_argument("--headless", action="store_true", help="render no window (default: a visible window)")
    runner.add_argument("--keep-running", action="store_true", help="leave the game running afterwards")
    checker = sub.add_parser("check", help="validate a scenario file and print its plan")
    checker.add_argument("file")
    options = parser.parse_args(argv)
    try:
        if options.command == "check":
            spec = load_scenario(options.file)
            print(json.dumps({"plan": plan(spec), "spec": spec}, indent=2, ensure_ascii=False))
            return 0
        return asyncio.run(run(options))
    except GameError as error:
        print(f"skyrim-devtools-scenario: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
