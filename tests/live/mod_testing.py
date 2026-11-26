"""Live check of the mod-testing tools (SPDX-License-Identifier: GPL-3.0-or-later).

Runs in the shared livetest session (`async def run(ctx)`). Starts a coverage session filtered to Skyrim.esm,
plays the start of A Lovely Letter, lists the topics of two Riverwood NPCs and explores Camilla, whose letter
choices (truth or lie) exclude each other. `--dry-run` checks the verdict logic on canned data without a game.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FAENDAL = "0001348C"
QUEST = "FreeformRiverwood01"
PLUGIN = "Skyrim.esm"
EXPLORE = "Camilla"
LUCAN = "Lucan Valerius"


def verdict(results):
    cov = results.get("coverage", {})
    explore = results.get("explore", {})
    branches = explore.get("branches", [])
    ok_branches = [b for b in branches if "error" not in b]
    checks = {
        "quest stage 10": results.get("questStage") == 10,
        "stage hit recorded": any(h.get("editorID") == QUEST and h.get("stage") == 10 for h in cov.get("stages", {}).get("hit", [])),
        "infos said recorded": len(cov.get("infos", {}).get("said", [])) > 0,
        "infos defined > covered": cov.get("infos", {}).get("defined", 0) > cov.get("infos", {}).get("covered", 0) > 0,
        "topics Lucan": results.get(f"topics:{LUCAN}", {}).get("topicTotal", 0) > 0,
        "topics Camilla": results.get(f"topics:{EXPLORE}", {}).get("topicTotal", 0) > 0,
        # Camilla offers two mutually exclusive choices, so both must be taken and the save loaded between them
        "both exclusive branches tried": len(ok_branches) >= 2 and all(b.get("stateChanged") for b in ok_branches[:2]),
        "save reloaded between branches": explore.get("reloads", 0) >= 1,
        "explore ended cleanly": explore.get("stopReason") == "done" and not explore.get("unexplored"),
    }
    summary = {
        "exploreBranches": len(branches), "exploreReloads": explore.get("reloads"), "exploreWallS": results.get("exploreWallS"),
        "topicsLucan": results.get(f"topics:{LUCAN}", {}).get("topicTotal"),
        "topicsCamilla": results.get(f"topics:{EXPLORE}", {}).get("topicTotal"),
        "stagesCovered": cov.get("stages", {}).get("covered"), "stagesDefined": cov.get("stages", {}).get("defined"),
        "infosCovered": cov.get("infos", {}).get("covered"), "infosDefined": cov.get("infos", {}).get("defined"),
        "papyrusErrors": cov.get("papyrus", {}).get("errorCount"), "papyrusWarnings": cov.get("papyrus", {}).get("warningCount"),
        "unreachable": {k: len(v) for k, v in explore.get("unreachable", {}).items()},
    }
    return {"pass": all(checks.values()), "details": {"checks": checks, "summary": summary}}


async def run(ctx):
    from skyrim_devtools.use.server import build_server

    server = build_server(ctx.manager)
    results = {}

    async def tool(name, **args):
        began = time.monotonic()
        content = await server.call_tool(name, args)
        content = content[0] if isinstance(content, tuple) else content
        ctx.mark(f"{name} {time.monotonic() - began:.1f}s")
        return json.loads(content[0].text)

    await ctx.setup("Fixture")
    results["coverageStart"] = await tool("coverage_start", plugins=[PLUGIN])
    # Faendal gives the letter, which unlocks Camilla's branch
    await ctx.call("Dialogue.start", refID=FAENDAL, wait=True)
    state = await ctx.call("Dialogue.waitIdle", skip=True)
    for wanted in ("problem with sven", "hope"):
        match = [t for t in state["topics"] if wanted in t["text"].lower()]
        assert match, f"topic {wanted!r} not offered, have {[t['text'] for t in state['topics']]}"
        await ctx.call("Dialogue.select", index=match[0]["index"])
        state = await ctx.call("Dialogue.waitIdle", skip=True)
    await ctx.call("Dialogue.close")
    results["questStage"] = (await ctx.call("Quest.get", editorID=QUEST))["stage"]

    await ctx.call("World.centerOnCell", cell="RiverwoodRiverwoodTrader")
    await asyncio.sleep(3)
    for npc in (LUCAN, EXPLORE):
        results[f"topics:{npc}"] = await tool("dialogue_topics", npc=npc, plugins=[PLUGIN], limit=400)
    began = time.monotonic()
    results["explore"] = await tool("dialogue_explore", npc=EXPLORE, strategy="unseen-first")
    results["exploreWallS"] = round(time.monotonic() - began, 1)
    results["coverage"] = await tool("coverage_report", limit=15, stop=True)
    out = verdict(results)
    out["details"]["markdown"] = results["explore"].get("markdown", "")[:1500]
    return out


CANNED = {
    "questStage": 10,
    "coverage": {"stages": {"hit": [{"editorID": QUEST, "stage": 10}], "defined": 9, "covered": 1},
                 "infos": {"said": [{"formID": "1"}], "defined": 5, "covered": 1}, "papyrus": {"errorCount": 0, "warningCount": 0}},
    f"topics:{LUCAN}": {"topicTotal": 3}, f"topics:{EXPLORE}": {"topicTotal": 2},
    "explore": {"branches": [{"stateChanged": True}, {"stateChanged": True}], "reloads": 1, "stopReason": "done",
                "unexplored": [], "unreachable": {"blockedByConditions": [], "passingButNotHeard": []}},
}

if __name__ == "__main__":
    if "--dry-run" not in sys.argv:
        raise SystemExit("run this through skyrim-devtools-livetest; --dry-run checks the verdict only")
    print(json.dumps(verdict(CANNED)))
