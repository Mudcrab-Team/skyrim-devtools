#!/usr/bin/env python3.13
"""Live check of the mod-testing tools (SPDX-License-Identifier: GPL-3.0-or-later).

Starts a coverage session filtered to Skyrim.esm, plays the start of A Lovely Letter, lists the topics of two
Riverwood NPCs and explores one conversation, then prints the coverage report.

    python3.13 tests/live/mod_testing.py --config devtools.json

Needs the Fixture save (Riverwood, A Lovely Letter unstarted). Prints {"pass", "details"} as the last line.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skyrim_devtools.use.manager import GameManager  # noqa: E402
from skyrim_devtools.use.server import build_server  # noqa: E402

FAENDAL = "0001348C"
QUEST = "FreeformRiverwood01"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--save", default="Fixture")
    parser.add_argument("--explore", default="Camilla")
    parser.add_argument("--plugin", default="Skyrim.esm")
    parser.add_argument("--out", default=str(Path.home() / ".skyrim-use" / "live-mod-testing.json"))
    options = parser.parse_args()
    manager = GameManager(json.loads(Path(options.config).expanduser().read_text()))
    server = build_server(manager)
    results = {}

    async def tool(name, **args):
        began = time.monotonic()
        content = await server.call_tool(name, args)
        content = content[0] if isinstance(content, tuple) else content
        value = json.loads(content[0].text)
        print(f"[{time.monotonic() - began:6.1f}s] {name} {json.dumps(args)[:100]}", flush=True)
        return value

    async def call(method, **params):
        return await manager.call(method, **params)

    try:
        await tool("game_start", headless=False, save=options.save)
        await tool("wait_for", playable=True, timeout_s=60)
        results["coverageStart"] = await tool("coverage_start", plugins=[options.plugin])

        # A Lovely Letter: Faendal gives the letter, which unlocks Camilla's branch.
        await call("Dialogue.start", refID=FAENDAL, wait=True)
        state = await call("Dialogue.waitIdle", skip=True)
        for wanted in ("problem with Sven", "hope"):
            match = [t for t in state["topics"] if wanted in t["text"].lower()]
            await call("Dialogue.select", index=match[0]["index"])
            state = await call("Dialogue.waitIdle", skip=True)
        await call("Dialogue.close")
        results["questStage"] = (await call("Quest.get", editorID=QUEST))["stage"]

        await call("World.centerOnCell", cell="RiverwoodRiverwoodTrader")
        await tool("wait_for", playable=True, timeout_s=60)
        await asyncio.sleep(2)
        for npc in ("Lucan Valerius", options.explore):
            began = time.monotonic()
            topics = await tool("dialogue_topics", npc=npc, plugins=[options.plugin], only_passing=False, limit=400)
            topics["tookS"] = round(time.monotonic() - began, 1)
            results[f"topics:{npc}"] = topics
        began = time.monotonic()
        results["explore"] = await tool("dialogue_explore", npc=options.explore, strategy="unseen-first")
        results["exploreWallS"] = round(time.monotonic() - began, 1)
        results["coverage"] = await tool("coverage_report", limit=15, stop=True)
    finally:
        Path(options.out).parent.mkdir(parents=True, exist_ok=True)
        Path(options.out).write_text(json.dumps(results, indent=1))
        print("results ->", options.out)
        await tool("game_stop")
    return results


def verdict(results):
    cov = results.get("coverage", {})
    explore = results.get("explore", {})
    checks = {
        "quest stage 10": results.get("questStage") == 10,
        "stage hit recorded": any(h.get("editorID") == QUEST and h.get("stage") == 10 for h in cov.get("stages", {}).get("hit", [])),
        "infos said recorded": len(cov.get("infos", {}).get("said", [])) > 0,
        "infos defined > covered": cov.get("infos", {}).get("defined", 0) > cov.get("infos", {}).get("covered", 0) > 0,
        "topics Lucan": results.get("topics:Lucan Valerius", {}).get("topicTotal", 0) > 0,
        "topics npc": results.get("topics:" + "Camilla", {}).get("topicTotal", 0) > 0,
        "explore branches": len(explore.get("branches", [])) >= 3,
        "explore ended cleanly": explore.get("stopReason") in ("done", "branch budget", "time budget"),
    }
    summary = {
        "exploreBranches": len(explore.get("branches", [])), "exploreReloads": explore.get("reloads"),
        "exploreWallS": results.get("exploreWallS"),
        "topicsLucan": results.get("topics:Lucan Valerius", {}).get("topicTotal"),
        "topicsCamilla": results.get("topics:Camilla", {}).get("topicTotal"),
        "stagesCovered": cov.get("stages", {}).get("covered"), "stagesDefined": cov.get("stages", {}).get("defined"),
        "infosCovered": cov.get("infos", {}).get("covered"), "infosDefined": cov.get("infos", {}).get("defined"),
        "papyrusErrors": cov.get("papyrus", {}).get("errorCount"), "papyrusWarnings": cov.get("papyrus", {}).get("warningCount"),
        "unreachable": {k: len(v) for k, v in explore.get("unreachable", {}).items()},
    }
    return {"pass": all(checks.values()), "details": {"checks": checks, "summary": summary}}


print(json.dumps(verdict(asyncio.run(main()))))
