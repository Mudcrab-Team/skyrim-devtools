#!/usr/bin/env python3
"""Run queued live test requests against one game session and write per-request reports.

  skyrim-devtools-livetest run REQUEST.yaml [--case NAME] --config devtools.json
  skyrim-devtools-livetest batch QUEUE_DIR --config devtools.json

The caller must already hold the PC lock (docs/test-pipeline.md); this tool never takes it.
A case script is either a module with `async def run(ctx) -> {"pass": bool, "details": {...}}` or a
standalone program that prints a JSON object as its last stdout line (the config path is passed in
SKYRIM_LIVETEST_CONFIG).
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import yaml

from .use.manager import GameError, GameManager
from .use.server import perf_verdict

PERF_INTERVAL = 15 * 60
CASE_TIMEOUT = 300.0
START_SETTLE = 8.0
START_RETRIES = 2
START_RETRY_WAIT = 25.0
CRASH_MARKERS = ("game is gone", "no game is running", "connection lost")


class Request:
    def __init__(self, data, path):
        self.path = Path(path)
        self.data = data
        self.id = str(data["id"])
        self.setup = data.get("setup") or ""
        self.fresh = bool(data.get("needs_fresh_game"))
        self.cases = list(data.get("cases") or [])
        for case in self.cases:
            if not isinstance(case, dict) or "name" not in case:
                raise ValueError(f"{path}: a case has no name")
        if not self.cases:
            raise ValueError(f"{path}: no cases")

    @property
    def touches(self):
        return set(self.data.get("touches") or [])


def load_request(path):
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError(f"{path}: not a test request (missing id)")
    return Request(data, path)


def load_queue(directory):
    """Requests in a queue directory, oldest file first; unreadable ones are returned separately."""
    requests, broken = [], []
    for path in sorted(Path(directory).glob("*.yaml"), key=lambda p: (p.stat().st_mtime, p.name)):
        try:
            requests.append(load_request(path))
        except (ValueError, yaml.YAMLError, OSError) as error:
            broken.append((path, str(error)))
    return requests, broken


def overlapping(requests):
    """Pairs of request ids whose `touches` share a file: a failure in one is ambiguous."""
    pairs = []
    for index, first in enumerate(requests):
        for second in requests[index + 1:]:
            shared = first.touches & second.touches
            if shared:
                pairs.append((first.id, second.id, sorted(shared)))
    return pairs


class Context:
    """What a live script sees: the client, setup, screenshots, timeline markers and events."""

    def __init__(self, manager, artifacts, request_id="", config_path=None):
        self.manager = manager
        self.artifacts = Path(artifacts)
        self.request_id = request_id
        self.config_path = config_path
        self.timeline = []
        self.screenshots = []
        self._started = time.monotonic()
        self._cursor = manager.event_total
        self.current_setup = ""

    @property
    def client(self):
        return self.manager.session

    async def call(self, method, timeout=None, **params):
        """A native call with the manager's reconnect handling."""
        return await self.manager.call(method, timeout=timeout, **params)

    def mark(self, text):
        self.timeline.append({"t": round(time.monotonic() - self._started, 1), "text": str(text)})

    async def setup(self, name):
        """Load a save by name, or `scenario:<name>` to apply a scenario through Scenario.setup."""
        self.mark(f"setup {name}")
        self.current_setup = name
        if name.startswith("scenario:"):
            result = await self.call("Scenario.setup", timeout=120, name=name.split(":", 1)[1])
        else:
            result = await self.call("Session.loadSave", timeout=120, name=name, wait=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = await self.call("Events.snapshot")
            if state.get("playable"):
                break
            await asyncio.sleep(0.5)
        else:
            raise GameError(f"setup {name}: world not playable after 90 s")
        self._cursor = self.manager.event_total
        return result

    async def screenshot(self, label):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
        path = self.artifacts / f"{self.request_id}-{safe}.png"
        await self.client.capture(path)
        self.screenshots.append(str(path))
        self.mark(f"screenshot {path.name}")
        return str(path)

    async def wait_event(self, name, timeout=10.0, match=None):
        """The next event of this type after the last setup or wait; None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            items, _ = self.manager.events_since(self._cursor)
            first = self.manager.event_total - len(items)
            for offset, event in enumerate(items):
                if event.get("type") == name and (match is None or match(event)):
                    self._cursor = first + offset + 1
                    return event
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def perf_check(self, seconds=10.0):
        return await perf_check(self.manager, seconds)


async def perf_check(manager, seconds=10.0):
    frame = (await manager.call("Perf.frameStats")).get("frame", 0)
    await asyncio.sleep(seconds)
    stats = await manager.call("Perf.frameStats", window=seconds)
    hitches = await manager.call("Perf.hitches", sinceFrame=frame, limit=10)
    return perf_verdict(stats, hitches, seconds)


def last_json(text):
    """The last JSON object in a script's output: one line, or an indented block starting at column 0."""
    lines = text.strip().splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("{"):
            try:
                value = json.loads("\n".join(lines[index:]))
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def split_command(script, config_path):
    """A case script is a path, optionally followed by arguments; `<mac.json>` stands for the config path
    and `--config` is appended when the script line has none."""
    words = shlex.split(script)
    config = str(config_path or "")
    words = [config if word in {"<mac.json>", "<config>"} else word for word in words]
    if config and "--config" not in words:
        words += ["--config", config]
    return words[0], words[1:]


async def run_script(ctx, script, root, timeout):
    name, args = split_command(script, ctx.config_path)
    path = (Path(root) / name).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"script {name} not found under {root}")
    module = None
    if not args and "async def run(" in path.read_text(encoding="utf-8", errors="replace"):
        spec = importlib.util.spec_from_file_location(f"livetest_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    if module is not None and hasattr(module, "run"):
        if not ctx.manager.alive:
            await ctx.manager.start(headless=False)
            if ctx.current_setup:
                await ctx.setup(ctx.current_setup)
        return await asyncio.wait_for(module.run(ctx), timeout)
    # a standalone program launches and stops its own game, so the shared session must be closed first
    if ctx.manager.alive:
        await ctx.manager.stop()
        await asyncio.sleep(START_SETTLE)
    env = {**os.environ, "SKYRIM_LIVETEST_CONFIG": str(ctx.config_path or "")}
    for attempt in range(START_RETRIES + 1):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(path), *args, env=env, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            data, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            raise
        text = data.decode("utf-8", "replace")
        # the host refuses a start while the previous game is still shutting down
        if "Windows host start failed" not in text or attempt == START_RETRIES:
            break
        ctx.mark(f"host start failed; retrying after {START_RETRY_WAIT:g} s")
        await asyncio.sleep(START_RETRY_WAIT)
    ctx.artifacts.mkdir(parents=True, exist_ok=True)
    log = ctx.artifacts / f"{ctx.request_id}-{path.stem}.log"
    log.write_text(text, encoding="utf-8")
    ctx.mark(f"script output {log.name}")
    result = last_json(text)
    if result is None:
        return {"pass": False, "details": {"error": "script printed no JSON object", "output": text[-600:]}}
    if "details" not in result:
        result = {"pass": bool(result.get("pass")), "details": {k: v for k, v in result.items() if k != "pass"}}
    return result


def uses_session(case, root, config_path=None):
    """True for a case that runs against the shared session: a module with `async def run` and no arguments,
    or a manual case (the harness takes its screenshot)."""
    if "script" not in case:
        return True
    name, args = split_command(case["script"], config_path)
    path = Path(root) / name
    if args or not path.is_file():
        return False
    return "async def run(" in path.read_text(encoding="utf-8", errors="replace")


def looks_like_crash(error):
    return any(marker in str(error) for marker in CRASH_MARKERS)


async def run_case(ctx, case, root, timeout=None):
    """One case's outcome: status pass/fail/error/crash/manual, details and wall time."""
    started = time.monotonic()
    budget = float(case.get("timeout_s") or timeout or CASE_TIMEOUT)
    outcome = {"name": case["name"], "expect": case.get("expect", ""), "status": "error", "details": {}}
    ctx.mark(f"case {case['name']} start")
    try:
        if "script" in case:
            result = await run_script(ctx, case["script"], root, budget)
            outcome["status"] = "pass" if result.get("pass") else "fail"
            outcome["details"] = result.get("details", {})
        else:
            outcome["status"] = "manual"
            outcome["details"] = {"steps": case.get("steps", ""), "judge": case.get("judge", "")}
            await ctx.screenshot(f"{case['name']}-state")
    except asyncio.TimeoutError:
        outcome["error"] = f"timed out after {budget:g} s"
    except (GameError, ConnectionError, OSError) as error:
        outcome["error"] = str(error)
        outcome["status"] = "crash" if looks_like_crash(error) else "error"
    except Exception as error:
        outcome["error"] = f"{type(error).__name__}: {error}"
    outcome["seconds"] = round(time.monotonic() - started, 1)
    if case.get("judge") == "visual" and "script" in case:
        try:
            await ctx.screenshot(f"{case['name']}-end")
        except Exception:
            pass
    ctx.mark(f"case {case['name']} {outcome['status']}")
    return outcome


def render_report(request, outcomes, timeline, notes, perf):
    passed = sum(o["status"] == "pass" for o in outcomes)
    lines = [f"# {request.id}", "",
             f"branch {request.data.get('branch', '?')} sha {request.data.get('sha', '?')} "
             f"implementer {request.data.get('implementer', '?')}", "",
             f"Result: {passed}/{len(outcomes)} pass", ""]
    for o in outcomes:
        lines += [f"## {o['name']}: {o['status']} ({o.get('seconds', 0)} s)", "", f"- expect: {o['expect']}"]
        if o.get("error"):
            lines.append(f"- error: {o['error']}")
        if o["details"]:
            lines.append(f"- measured: `{json.dumps(o['details'], sort_keys=True)}`")
        if o.get("rerun"):
            lines.append(f"- isolated rerun: {o['rerun']}")
        lines.append("")
    if timeline:
        lines += ["## Timeline", ""] + [f"- {m['t']} s {m['text']}" for m in timeline] + [""]
    if perf:
        lines += ["## Session perf", ""] + [f"- {json.dumps(p, sort_keys=True)}" for p in perf] + [""]
    if notes:
        lines += ["## Tester notes", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


class Batch:
    def __init__(self, manager, results_dir, root, config_path=None, perf_interval=PERF_INTERVAL, perf_seconds=10.0, perf_save=""):
        self.manager = manager
        self.results = Path(results_dir)
        self.root = Path(root)
        self.config_path = config_path
        self.perf_interval = perf_interval
        self.perf_seconds = perf_seconds
        self.perf_save = perf_save
        self.perf = []
        self.last_perf = None
        self.notes = {}
        self.outcomes = {}
        self.timelines = {}

    def note(self, request_id, text):
        self.notes.setdefault(request_id, []).append(text)

    def new_context(self, request):
        return Context(self.manager, self.results / "artifacts", request.id, self.config_path)

    async def ensure_game(self):
        if not self.manager.alive:
            await self.manager.start(headless=False)
            self.last_perf = None

    async def maybe_perf(self, ctx, label):
        if self.last_perf is not None and time.monotonic() - self.last_perf < self.perf_interval:
            return
        try:
            reading = await ctx.perf_check(self.perf_seconds)
            keep = ("fps", "p50Ms", "p99Ms", "maxMs", "hitchCount", "warnings")
            self.perf.append({"at": label, **{k: reading.get(k) for k in keep}})
        except Exception as error:
            self.perf.append({"at": label, "error": str(error)})
        self.last_perf = time.monotonic()

    def plan(self, requests):
        """Request order for the game: setup groups together, needs_fresh_game requests last."""
        normal = [r for r in requests if not r.fresh]
        order = {}
        for request in normal:
            order.setdefault(request.setup, len(order))
        return sorted(normal, key=lambda r: order[r.setup]) + [r for r in requests if r.fresh]

    def perf_due(self):
        return self.last_perf is None or time.monotonic() - self.last_perf >= self.perf_interval

    async def run(self, requests, only_case=None, case_timeout=None):
        """The shared session is started only for cases that use it and for due perf readings; standalone
        scripts bring their own game."""
        loaded = None
        for request in self.plan(requests):
            if request.fresh and self.manager.alive:
                await self.manager.stop()
                loaded = None
            ctx = self.new_context(request)
            self.timelines[request.id] = ctx.timeline
            results = []
            self.outcomes[request.id] = results
            for case in request.cases:
                if only_case and case["name"] != only_case:
                    continue
                session_case = uses_session(case, self.root, self.config_path)
                if session_case or self.perf_due():
                    # a standalone script loads its own save; the game started for a perf reading only needs a world
                    wanted = request.setup if session_case else self.perf_save
                    try:
                        was_alive = self.manager.alive
                        await self.ensure_game()
                        if wanted and (loaded != wanted or not was_alive):
                            await ctx.setup(wanted)
                            loaded = wanted
                    except Exception as error:
                        loaded = None
                        self.note(request.id, f"setup {wanted} failed: {error}")
                        results.append({"name": case["name"], "expect": case.get("expect", ""), "status": "error",
                                        "details": {}, "error": f"setup failed: {error}", "seconds": 0})
                        continue
                    await self.maybe_perf(ctx, f"before {request.id}/{case['name']}")
                outcome = await run_case(ctx, case, self.root, case_timeout)
                results.append(outcome)
                if not self.manager.alive:
                    loaded = None
                if outcome["status"] == "crash":
                    self.note(request.id, f"case {case['name']}: game lost ({outcome.get('error')}); restarting for the next case")
                    await self.manager.stop()
                    loaded = None
        return self.outcomes

    def write_reports(self, requests):
        self.results.mkdir(parents=True, exist_ok=True)
        pairs = overlapping(requests)
        written = []
        for request in requests:
            if request.id not in self.outcomes:
                continue
            notes = list(self.notes.get(request.id, []))
            bad = any(o["status"] in {"fail", "error", "crash"} for o in self.outcomes[request.id])
            for first, second, files in pairs:
                if bad and request.id in (first, second):
                    other = second if request.id == first else first
                    notes.append(f"failure is ambiguous: shares {files} with {other}; rerun in isolation")
            path = self.results / f"{request.id}.md"
            path.write_text(render_report(request, self.outcomes[request.id], self.timelines.get(request.id, []),
                                          notes, self.perf), encoding="utf-8")
            written.append(path)
        return written


def summary_line(request, outcomes):
    counts = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    return f"{request.id}: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no cases run")


async def amain(options):
    manager = GameManager(json.loads(options.config.read_text()))
    if options.command == "run":
        requests, broken = [load_request(options.target)], []
    else:
        requests, broken = load_queue(options.target)
    for path, error in broken:
        print(f"skipped {path.name}: {error}", file=sys.stderr)
    if not requests:
        print("no requests")
        return 1
    batch = Batch(manager, options.results, options.root, options.config, perf_save=options.perf_save)
    try:
        await batch.run(requests, getattr(options, "case", None), options.case_timeout)
    finally:
        for path in batch.write_reports(requests):
            print(f"report {path}")
        if not options.keep:
            await manager.stop()
    for request in requests:
        print(summary_line(request, batch.outcomes.get(request.id, [])))
    return 1 if any(o["status"] not in {"pass", "manual"} for outs in batch.outcomes.values() for o in outs) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "batch"])
    parser.add_argument("target", type=Path, help="request file (run) or queue directory (batch)")
    parser.add_argument("--config", type=Path, default=os.environ.get("SKYRIM_DEVTOOLS_CONFIG"))
    parser.add_argument("--case", help="run only this case (run)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="checkout the case scripts are resolved against")
    parser.add_argument("--results", type=Path, help="report directory (default: <queue>/results)")
    parser.add_argument("--case-timeout", type=float, default=None, help="seconds per case unless the case sets timeout_s")
    parser.add_argument("--perf-save", default="", help="save to load for perf readings taken before standalone scripts")
    parser.add_argument("--keep", action="store_true", help="leave the game running afterwards")
    options = parser.parse_args(argv)
    if not options.config:
        parser.error("pass --config or set SKYRIM_DEVTOOLS_CONFIG")
    if options.results is None:
        queue = options.target if options.command == "batch" else options.target.parent
        options.results = queue / "results"
    return asyncio.run(amain(options))


if __name__ == "__main__":
    raise SystemExit(main())
