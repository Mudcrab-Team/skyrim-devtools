"""Mod-testing tools: the dialogue explorer and the play-session coverage report.

`DialogueExplorer` walks every branch of an NPC's conversation. It only needs an `ops` object with a few async
methods, so the walk is tested against a simulated conversation. Plain game saves undo choices that exclude
each other: the explorer saves once before the first line and loads that save whenever a branch changed the
world (a stage was set, an item moved, the topic list changed) before it tries the next sibling.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import time

from .manager import GameError

HEX_ID = re.compile(r"(0x)?[0-9A-Fa-f]{8}")
STATE_EVENTS = ("questStage", "containerChanged")
MAX_DEPTH = 6
GREETING = "(greeting)"


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def topic_signature(topics):
    """What the player sees, without the said-before flags that every visit changes."""
    return tuple(sorted(t["text"] for t in topics))


def pick_next(queue, strategy):
    """Choose the next branch. breadth: shallowest first. unseen-first: topics never said first, then the
    deepest branch so the conversation is followed instead of restarted."""
    if strategy == "breadth":
        return min(queue, key=lambda w: (len(w["path"]), w["seq"]))
    return min(queue, key=lambda w: (not w["neverSaid"], -len(w["path"]), w["seq"]))


class DialogueExplorer:
    def __init__(self, ops, strategy="unseen-first", max_branches=80, time_budget_s=900.0, clock=time.monotonic):
        if strategy not in ("breadth", "unseen-first"):
            raise GameError("strategy is breadth or unseen-first")
        self.ops = ops
        self.strategy = strategy
        self.max_branches = max_branches
        self.time_budget_s = time_budget_s
        self.clock = clock
        self.branches = []
        self.queue = []
        self.nodes = {}
        self.signatures = {}
        self.said = {}
        self.position = None  # path of the topic list on screen, None outside a conversation
        # dirty: the world differs from "save + the choices on screen's path", so the screen cannot be reused.
        # worldChanged: anything changed since the save, so a restart needs a reload first.
        self.dirty = False
        self.world_changed = False
        self.reloads = 0
        self.selects = 0
        self.seq = 0
        self.stop_reason = "done"

    def record_said(self, infos):
        for info in infos:
            self.said.setdefault(info["info"], {"info": info["info"], "topicText": info.get("topicText"),
                                                "quest": info.get("questEditorID"), "plugin": info.get("plugin")})

    async def open(self):
        """Start the conversation and return the first topic list (greeting lines go to the root node)."""
        step = await self.ops.start()
        self.position = ()
        self.selects += 1
        return step

    def enqueue(self, path, topics):
        for topic in topics:
            self.seq += 1
            self.queue.append({"path": path, "choice": topic["text"], "neverSaid": bool(topic.get("neverSaid")),
                               "topic": topic.get("topic"), "info": topic.get("info"), "seq": self.seq})

    async def goto(self, path):
        """Put the conversation on the topic list reached by `path`, with the world as the save left it."""
        if not self.dirty and self.position is not None and path[:len(self.position)] == self.position:
            remaining = path[len(self.position):]
        else:
            if self.position is not None:
                await self.ops.close()
            if self.dirty or self.world_changed:
                await self.ops.reload()
                self.reloads += 1
                self.dirty = self.world_changed = False
            await self.open()
            remaining = path
        for choice in remaining:
            step = await self.ops.select(choice)
            self.selects += 1
            self.world_changed = self.world_changed or bool(step.get("stateEvents"))
            if step["status"] == "closed" or step["status"] == "submenu":
                raise GameError(f"replaying {list(path)} ended the conversation at {choice!r}")
            self.position = (*self.position, choice)

    async def run(self):
        started = self.clock()
        await self.ops.save()
        step = await self.open()
        self.record_said(step.get("said", []))
        self.nodes[()] = {"path": [], "topics": [t["text"] for t in step["topics"]], "greeting": step.get("lines", [])}
        self.signatures[topic_signature(step["topics"])] = ()
        self.enqueue((), step["topics"])
        try:
            while self.queue:
                if len(self.branches) >= self.max_branches:
                    self.stop_reason = "branch budget"
                    break
                if self.clock() - started > self.time_budget_s:
                    self.stop_reason = "time budget"
                    break
                work = pick_next(self.queue, self.strategy)
                self.queue.remove(work)
                try:
                    await self.goto(work["path"])
                except GameError as error:
                    self.branches.append({"path": list(work["path"]), "choice": work["choice"], "error": str(error)})
                    self.position = None
                    continue
                await self.try_branch(work)
        finally:
            with contextlib.suppress(Exception):
                await self.ops.close()
        return {
            "strategy": self.strategy, "elapsedS": round(self.clock() - started, 1), "stopReason": self.stop_reason,
            "branches": self.branches, "nodes": list(self.nodes.values()), "reloads": self.reloads,
            "selects": self.selects, "saidInfos": list(self.said.values()),
            "unexplored": [{"path": list(w["path"]), "choice": w["choice"]} for w in self.queue],
        }

    async def try_branch(self, work):
        parent = self.nodes[work["path"]]
        before = set(topic_signature([{"text": t} for t in parent["topics"]]))
        step = await self.ops.select(work["choice"])
        self.selects += 1
        self.record_said(step.get("said", []))
        status = step["status"]
        changed = bool(step.get("stateEvents"))
        next_topics = step["topics"] if status == "topics" else []
        entry = {"path": list(work["path"]), "choice": work["choice"], "status": status,
                 "lines": step.get("lines", []), "info": [i["info"] for i in step.get("said", [])],
                 "next": [t["text"] for t in next_topics]}
        if status == "topics":
            signature = topic_signature(next_topics)
            shown = set(signature)
            if shown == before:
                self.position = work["path"]
                entry["returnsTo"] = "same list"
            elif signature in self.signatures:
                self.position = self.signatures[signature]
                entry["sameListAs"] = list(self.position)
            else:
                child_path = (*work["path"], work["choice"])
                overlap = len(shown & before) / max(1, len(shown))
                # a list that mostly repeats the parent's but differs means the choice switched options on or off
                if overlap >= 0.6:
                    changed = True
                    fresh = [t for t in next_topics if t["text"] not in before]
                else:
                    fresh = next_topics
                self.signatures[signature] = child_path
                self.nodes[child_path] = {"path": list(child_path), "topics": [t["text"] for t in next_topics]}
                if len(child_path) < MAX_DEPTH:
                    self.enqueue(child_path, fresh)
                self.position = child_path
        else:
            self.position = None
            if status == "submenu":
                entry["submenu"] = step.get("submenu")
            if status != "closed":
                await self.ops.close()
        entry["stateChanged"] = changed
        if changed:
            self.world_changed = True
            # the choice's effect sits on the trail only when the conversation moved on to its own list
            if status != "topics" or self.position != (*work["path"], work["choice"]):
                self.dirty = True
        self.branches.append(entry)


class GameOps:
    """The conversation primitives of a running game, over the plugin's RPC methods."""

    def __init__(self, manager, native, ref_id, save_name, wait_ms=20000):
        self.manager = manager
        self.native = native
        self.ref_id = ref_id
        self.save_name = save_name
        self.wait_ms = wait_ms
        self.cursor = manager.event_total
        self.topics_by_text = {}

    def events(self, types):
        items, _ = self.manager.events_since(self.cursor)
        return [e for e in items if e["type"] in types]

    def advance_cursor(self):
        self.cursor = self.manager.event_total

    async def settle(self):
        """After a select or start: wait for the next list, then describe what happened."""
        result = await self.native("Dialogue.waitIdle", timeoutMs=self.wait_ms, skip=True)
        status = result.get("status")
        topics = []
        if status in ("topics", "idle"):
            offered = await self.native("Dialogue.offered")
            topics = [{"index": t["index"], "text": t["text"], "neverSaid": t["neverSaid"], "topic": t["topic"],
                       "info": t["info"]} for t in offered.get("topics", [])]
            status = "topics" if topics else "idle"
        said = [e["data"] for e in self.events(("dialogueInfo",))]
        state_events = [e["data"] for e in self.events(STATE_EVENTS)]
        self.advance_cursor()
        lines = [line["text"] for line in result.get("lines", [])]
        return {"status": status, "topics": topics, "lines": lines, "said": said, "stateEvents": state_events,
                "submenu": result.get("submenu")}

    async def start(self):
        await self.native("Dialogue.start", refID=self.ref_id, wait=True)
        step = await self.settle()
        self.current = step["topics"]
        return step

    async def select(self, text):
        topics = self.current
        match = [t for t in topics if t["text"] == text]
        if not match:
            raise GameError(f"topic {text!r} is not offered (have {[t['text'] for t in topics]})")
        await self.native("Dialogue.select", index=match[0]["index"])
        step = await self.settle()
        self.current = step["topics"]
        return step

    async def close(self):
        await self.native("Dialogue.close")
        self.current = []

    async def save(self):
        await self.manager.call("Console.execute", command=f"save {self.save_name}", target="player", timeout=20)
        for _ in range(40):
            saves = await self.manager.call("Session.listSaves")
            if any(item.get("name") == self.save_name for item in saves):
                return
            await asyncio.sleep(0.5)
        raise GameError("the game did not write the exploration save")

    async def reload(self):
        await self.manager.call("Session.loadSave", timeout=120, name=self.save_name, wait=True)
        for _ in range(240):
            snap = await self.manager.call("Events.snapshot")
            names = {m["name"] for m in snap.get("menus", [])}
            if snap.get("playable") and "Fader Menu" not in names:
                return
            await asyncio.sleep(0.25)
        raise GameError("the game was not playable after loading the exploration save")


def relevant_topics(topics_result):
    """Player-reachable infos of a Dialogue.topics reply as {formID: info + topic context}."""
    out = {}
    for topic in topics_result.get("topics", []):
        if topic.get("kind") not in ("player", "scene", "favors", "service", "command"):
            continue
        for info in topic.get("infos", []):
            out[info["formID"]] = {**info, "topicText": topic.get("text"), "topic": topic["formID"],
                                   "quest": (topic.get("quest") or {}).get("editorID"),
                                   "questPlugin": (topic.get("quest") or {}).get("plugin"),
                                   "questRunning": (topic.get("quest") or {}).get("running"), "kind": topic.get("kind")}
    return out


def unreachable(topics_result, said_infos):
    """Infos addressed to the NPC (player-facing kinds) that exploring never heard, split by why."""
    heard = {item["info"] for item in said_infos}
    blocked, not_reached = [], []
    for form_id, info in relevant_topics(topics_result).items():
        if form_id in heard:
            continue
        brief = {k: info.get(k) for k in ("formID", "topicText", "quest", "plugin", "kind", "text", "sayOnce", "said")}
        if info.get("passes"):
            not_reached.append(brief)
        else:
            brief["failing"] = info.get("failing", []) + info.get("questFailing", [])
            brief["questRunning"] = info.get("questRunning")
            blocked.append(brief)
    return {"blockedByConditions": blocked, "passingButNotHeard": not_reached}


def explore_markdown(npc, report, reach):
    lines = [f"# Dialogue exploration: {npc}", "",
             f"{report['strategy']}: {len(report['branches'])} branches in {report['elapsedS']} s "
             f"({report['reloads']} save reloads, {report['selects']} selections), stopped: {report['stopReason']}.", ""]
    lines.append("## Tree traversed")
    for branch in report["branches"]:
        indent = "  " * len(branch["path"])
        if "error" in branch:
            lines.append(f"{indent}- {branch['choice']}: could not reach ({branch['error']})")
            continue
        flags = [branch["status"]] + (["state changed"] if branch.get("stateChanged") else [])
        said = " / ".join(branch["lines"][:2])
        lines.append(f"{indent}- **{branch['choice']}** [{', '.join(flags)}] {said}")
    if report["unexplored"]:
        lines += ["", f"{len(report['unexplored'])} branches left unexplored by the budget."]
    lines += ["", f"## Never reachable under the current state", ""]
    for key, title in (("blockedByConditions", "Blocked by conditions"), ("passingButNotHeard", "Conditions pass but never heard")):
        items = reach[key]
        lines.append(f"{title}: {len(items)}")
        for item in items[:25]:
            lines.append(f"- {item['quest']} / {item['topicText']} ({item['formID']}, {item['plugin']})"
                         + (f" failing {item['failing']}" if item.get("failing") else ""))
    return "\n".join(lines)


async def resolve_npc(native, npc):
    """A reference id as given, or the nearest actor whose name contains `npc`."""
    if HEX_ID.fullmatch(npc):
        return npc.replace("0x", ""), None
    nearby = await native("World.nearby", radius=6000, types=["actor"], limit=200)
    refs = nearby.get("refs", nearby if isinstance(nearby, list) else [])
    wanted = npc.lower()
    exact = [r for r in refs if (r.get("name") or "").lower() == wanted]
    partial = [r for r in refs if wanted in (r.get("name") or "").lower()]
    found = (exact or partial or [None])[0]
    if not found:
        raise GameError(f"no loaded actor named {npc!r} within 6000 units; walk there first or pass a refID")
    return found["refID"], found.get("name")


# ---- coverage -----------------------------------------------------------------------------------------

def coverage_markdown(report, event_deltas=None):
    stages, infos, scenes, papyrus = report["stages"], report["infos"], report["scenes"], report["papyrus"]
    plugins = ", ".join(report["plugins"]) or "all plugins"
    out = [f"# Coverage report ({plugins})", "", f"Session length {report['durationS']} s.", "",
           "| Area | Covered | Defined |", "| --- | --- | --- |",
           f"| Quest stages | {stages['covered']} | {stages['defined']} in {stages['quests']} quests |",
           f"| Dialogue infos | {infos['covered']} | {infos['defined']} |",
           f"| Scenes started | {len(scenes['started'])} | {scenes['defined']} |",
           f"| Quests started | {len(report['questsStarted'])} | |", "",
           f"## Quest stages set ({len(stages['hit'])})"]
    for hit in stages["hit"][:100]:
        out.append(f"- t+{hit['atS']} s {hit['editorID'] or hit['formID']} stage {hit['stage']} ({hit['plugin']})")
    out += ["", f"## Dialogue infos said ({len(infos['said'])})"]
    for item in infos["said"][:100]:
        out.append(f"- {item['quest']} / {item['topicText']} {item['formID']} ({item['via']})")
    out += ["", f"## Scenes started ({len(scenes['started'])})"]
    for item in scenes["started"]:
        out.append(f"- t+{item['atS']} s {item['editorID'] or item['formID']} ({item['plugin']})")
    out += ["", f"## Papyrus: {papyrus['errorCount']} errors, {papyrus['warningCount']} warnings"]
    for item in papyrus["errors"][:30] + papyrus["warnings"][:30]:
        out.append(f"- t+{item['atS']} s [{item['severity']}] {item['text'][:200]}")
    out += ["", f"Scripts that logged: {', '.join(s['script'] for s in papyrus['scriptsLogging'][:40]) or 'none'}"]
    if event_deltas:
        out += ["", "Engine events seen by script event sinks: " + ", ".join(f"{k} {v}" for k, v in sorted(event_deltas.items()) if v)]
    return "\n".join(out)


def counts_delta(before, after):
    return {k: after[k]["seen"] - (before.get(k) or {"seen": 0})["seen"] for k in after}


class CoverageSession:
    def __init__(self, recorder, manager):
        self.recorder = recorder
        self.manager = manager
        self.baseline = None
        self.task = None
        self.cursor = 0

    async def start(self, native, plugins):
        result = await native("Coverage.start", plugins=plugins or None)
        with contextlib.suppress(Exception):
            self.baseline = await native("GameEvents.counts")
        self.cursor = self.manager.event_total
        self.recorder.write({"t": "coverage", "ms": int(time.time() * 1000), "action": "start", "plugins": plugins or []})
        if self.task is None:
            self.task = asyncio.create_task(self._forward())
        return result

    async def stop_forwarding(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.forward_once()

    def forward_once(self):
        """Copy Papyrus problems from the event buffer into the timeline."""
        events, _ = self.manager.events_since(self.cursor)
        self.cursor = self.manager.event_total
        for event in events:
            if event["type"] == "papyrusError":
                self.recorder.write({"t": "papyrusError", "ms": int(time.time() * 1000), **event["data"]})

    async def _forward(self):
        while True:
            await asyncio.sleep(1.0)
            self.forward_once()


def register(mcp, native, manager, recorder):
    coverage = CoverageSession(recorder, manager)
    out_dir = Path(os.environ.get("SKYRIM_USE_COVERAGE_DIR") or recorder.directory.parent / "coverage")

    @mcp.tool()
    async def dialogue_topics(npc: str, include_generic: bool = False, only_passing: bool = False,
                              plugins: list[str] | None = None, quest: str | None = None,
                              kinds: list[str] | None = None, limit: int = 60, responses: bool = True) -> str:
        """Every dialogue topic and info an NPC could say or offer, from the quest/topic/info records (Dialogue.topics).
        `npc` is a reference id or part of the name of a loaded actor. Each topic: text, kind (player, scene,
        combat, favors, detection, service, misc), owning quest (editorID, plugin, running) and infos with
        formID, plugin, `passes` (the engine evaluated the quest and info conditions against this NPC and the
        player; `failing` lists the condition function indexes that were false), `said` (the info was
        spoken before) and the first response line. Lines addressed to other NPCs are left out; include_generic=true
        adds lines with no speaker condition. Filter with plugins (mod names), quest (substring) and kinds."""
        ref_id, name = await resolve_npc(native, npc)
        result = await native("Dialogue.topics", refID=ref_id, includeGeneric=include_generic, onlyPassing=only_passing,
                              plugins=plugins, quest=quest, kinds=kinds, limit=limit, responses=responses)
        result["resolvedName"] = name
        return dumps(result)

    @mcp.tool()
    async def dialogue_explore(npc: str, strategy: str = "unseen-first", max_branches: int = 60,
                               time_budget_s: float = 600) -> str:
        """Talk to an NPC and try every topic branch. breadth tries all topics of a list before going deeper;
        unseen-first starts with topics never said and follows a conversation down. A plain save is made first
        and loaded again whenever a branch changed the world (quest stage, items) or the topic list, so
        mutually exclusive choices all get tried. Returns the tree traversed (lines heard, whether each choice
        changed state), the infos heard, and the lines addressed to the NPC that were not reachable under the
        starting state: blocked by conditions (with the failing function indexes) or passing but never heard.
        The player must stand near a loaded NPC. Leaves the conversation closed and the world as loaded from
        the save (the save is kept as explore_<ref>)."""
        ref_id, name = await resolve_npc(native, npc)
        topics = await native("Dialogue.topics", refID=ref_id, responses=True, limit=2000)
        ops = GameOps(manager, native, ref_id, f"explore_{ref_id[-6:].lower()}")
        explorer = DialogueExplorer(ops, strategy=strategy, max_branches=max_branches, time_budget_s=time_budget_s)
        report = await explorer.run()
        reach = unreachable(topics, report["saidInfos"])
        report["unreachable"] = reach
        report["npc"] = {"refID": ref_id, "name": name}
        label = name or ref_id
        markdown = explore_markdown(label, report, reach)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out_dir / f"explore-{ref_id}-{stamp}.json").write_text(json.dumps(report, indent=1))
        (out_dir / f"explore-{ref_id}-{stamp}.md").write_text(markdown)
        report["markdown"] = markdown
        return dumps(report)

    @mcp.tool()
    async def coverage_start(plugins: list[str] | None = None) -> str:
        """Begin recording coverage for a play session (Coverage.start), filtered to the named plugins (mod file
        names such as MyMod.esp; empty means every plugin). Records quest stages set, dialogue infos spoken,
        scenes started, quests started and Papyrus warnings/errors/script log activity. Papyrus problems are
        also written to the timeline as papyrusError lines. Restarting resets the session."""
        return dumps(await coverage.start(native, plugins))

    @mcp.tool()
    async def coverage_report(limit: int = 100, stop: bool = False) -> str:
        """Report coverage so far (Coverage.report): stages set, infos said, scenes and quests started, Papyrus
        errors and warnings, each against what the chosen plugins define (covered vs defined, plus the missed
        ones up to `limit`). Writes JSON and markdown files under the coverage folder and returns the report
        with a `markdown` summary. stop=true ends the session."""
        report = await native("Coverage.report", limit=limit, stop=stop)
        deltas = None
        if coverage.baseline is not None:
            with contextlib.suppress(Exception):
                deltas = counts_delta(coverage.baseline, await native("GameEvents.counts"))
        report["eventSinks"] = deltas
        coverage.forward_once()
        if stop:
            await coverage.stop_forwarding()
        report["markdown"] = coverage_markdown(report, deltas)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out_dir / f"coverage-{stamp}.json").write_text(json.dumps(report, indent=1))
        (out_dir / f"coverage-{stamp}.md").write_text(report["markdown"])
        report["files"] = str(out_dir / f"coverage-{stamp}")
        return dumps(report)
