"""Profiler tools: sample the game's threads, then get a symbolised report and Firefox Profiler / flame graph files.

The report names game functions from a local skyrim-research checkout (config "researchCheckout" or the
SKYRIM_DEVTOOLS_RESEARCH variable); without one it falls back to Address Library IDs and sub_<rva>.
"""

import asyncio
import json
from pathlib import Path
import time

from .. import profile_gpa, profile_sqlite, profile_trace, profile_views, profiling


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def register(mcp, native, manager, recorder=None):
    state = {"symbols": None, "sqlite": None, "gpuCounters": None, "raw": None}

    def symbols():
        if state["symbols"] is None:
            state["symbols"] = profiling.GameSymbols(profiling.research_root(config=manager.config))
        return state["symbols"]

    @mcp.tool()
    async def profile_start(threads: str = "all", hz: float = 1000.0, duration_s: float = 30.0,
                            gpu_counters: bool = False, trace: str = "batched") -> str:
        """Start the sampling CPU profiler (Profile.start). threads: main (the game thread), frame (the thread
        that presents), all, or comma-separated thread ids / name fragments. hz 10-4000 (default 1000);
        duration_s caps the run (max 600). Do the thing to profile, then call profile_stop. gpu_counters: on an
        AMD GPU with GPUPerfAPI installed (config gpaDir), profile_stop also captures hardware counters for a
        few seconds after the CPU profile (see profile_gpu_counters). trace: batched (default; engine phases,
        D3D11 calls summed per render pass, OS waits and file reads), full (every D3D11 call) or off; the trace
        lands in trace.pftrace and the TraceEvents table (see profile_trace_summary)."""
        state["gpuCounters"] = gpu_counters
        params = {}
        if trace != "off":
            lists = [p for p in (manager.config.get("traceHooks"), Path.cwd() / ".local" / "trace-hooks.json") if p]
            hooks, _ = await asyncio.to_thread(profile_trace.local_hooks, lists, symbols())
            params["trace"] = profile_trace.trace_config(api=trace, hooks=hooks)
        return dumps(await native("Profile.start", threads=threads, hz=hz, durationS=duration_s, **params))

    @mcp.tool()
    async def profile_stop(top: int = 10, out_dir: str | None = None) -> str:
        """Stop the profiler and return the report: top functions by self and total time (on-CPU), hottest
        stacks, the module split (game / plugin / skse / gpu-driver / kernel / system / other-dll), what the
        thread waited on, slow frames (>100 ms) with their stacks and wait chain, sampler overhead and the
        share of game samples with a real function name. Also writes raw data, a Firefox Profiler profile
        (profile.json.gz, open at profiler.firefox.com), trace.pftrace (open locally in ui.perfetto.dev), flame graph SVGs and timeline.html (threads, frames, cell loads, tool calls and
        hitches on one zoomable time axis), and profile.sqlite (for profile_query) to out_dir (default
        .local/profiles/<stamp> on this machine) and returns their paths. The gpu section has per-frame GPU time,
        passes, whether frames were GPU- or CPU-bound and, on AMD with the ADLX helper, board metrics (clocks, power,
        temperature, fan, VRAM); gpuCounters holds the GPUPerfAPI result when profile_start asked for it."""
        raw, _ = await profiling.fetch(native)
        if state["gpuCounters"]:
            state["gpuCounters"] = False
            raw["gpaCounters"] = await gpa(None, None, 600)
        if recorder is not None and raw.get("startEpochMs"):
            raw.setdefault("marks", []).extend(profile_views.recorder_marks(
                recorder.path, raw["startEpochMs"], raw.get("durationS", 0) * 1000.0))
        profile = profiling.Profile(raw, symbols())
        out = Path(out_dir).expanduser() if out_dir else profiling.default_out()
        paths, summary = profiling.save_outputs(profile, out)
        summary["files"] = paths
        state["sqlite"] = paths.get("sqlite")
        state["raw"] = paths.get("raw")
        for key in ("main", "all"):
            if key in summary:
                for field in ("topSelf", "topTotal"):
                    summary[key][field] = summary[key][field][:top]
        return dumps(summary)

    async def gpa(directory, counters, max_frames):
        directory = profile_gpa.gpa_dir(directory, manager.config)
        if not directory:
            return {"ok": False, "error": "GPUPerfAPI folder unknown: set config gpaDir or SKYRIM_DEVTOOLS_GPA_DIR"}
        try:
            return await profile_gpa.capture(native, directory, counters, max_frames)
        except Exception as error:  # noqa: BLE001 - reported to the caller as data
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    @mcp.tool()
    async def profile_gpu_counters(action: str = "capture", counters: list[str] | None = None, max_frames: int = 600,
                                   gpa_dir: str | None = None) -> str:
        """AMD hardware GPU counters through GPUPerfAPI (opt-in; needs GPA's DLLs on the game PC, config gpaDir).
        action "list": counters this GPU and D3D11 offer, with the default set. action "capture": one frame per
        counter pass (GPA reads a few counters per pass), so values combine consecutive frames of the same scene;
        returns per-counter frame values (GPU busy, shader busy per stage, cache hit rates, memory traffic, stalls),
        the heaviest clear-delimited samples and the fps cost while capturing. Does not touch a running CPU profile's
        samples beyond the slower frames."""
        directory = profile_gpa.gpa_dir(gpa_dir, manager.config)
        if action == "list":
            if not directory:
                return dumps({"ok": False, "error": "GPUPerfAPI folder unknown: set config gpaDir or SKYRIM_DEVTOOLS_GPA_DIR"})
            return dumps(await profile_gpa.list_counters(native, directory))
        result = await gpa(directory, counters, max_frames)
        return dumps(profile_gpa.summarize(result) if result.get("ok") else result)

    @mcp.tool()
    async def profile_status() -> str:
        """Whether a profile or the hitch watch is running, with sample counts so far (Profile.status)."""
        return dumps(await native("Profile.status"))

    @mcp.tool()
    async def profile_watch(enabled: bool = True, hz: float | None = None, hitch_ms: float | None = None) -> str:
        """Keep sampling the main thread at a low rate (default 250 Hz) so any frame slower than hitch_ms
        (default 100) keeps its stacks; read them with profile_hitches (Profile.watch)."""
        return dumps(await native("Profile.watch", enabled=enabled, hz=hz, hitchMs=hitch_ms))

    @mcp.tool()
    async def profile_hitches(limit: int | None = None, since_frame: int | None = None, clear: bool | None = None) -> str:
        """Slow frames caught by profile_watch or a running profile, each with where the main thread spent it
        (top self functions), what it waited on, its dominant stack and, if it was blocked, the Windows wait
        chain (which thread/lock it waited for). Frame numbers match perf_hitches."""
        raw = await native("Profile.hitches", limit=limit, sinceFrame=since_frame, clear=clear)
        profile = profiling.Profile({"modules": raw.get("modules", []), "hitches": raw.get("hitches", []),
                                     "hitchFrames": raw.get("frames", []), "frames": [], "stacks": []}, symbols())
        return dumps({"status": raw.get("status"),
                      "hitches": profile.hitches(), "credits": profile.credits() or None, "at": time.time()})

    @mcp.tool()
    async def profile_top(seconds: float = 3.0, top: int = 15) -> str:
        """Live snapshot without stopping anything, like nvidia-smi for the game: fps and frame time p50/p99,
        main thread busy vs waiting %, its top functions by self % over the last `seconds`, per-thread CPU %,
        process memory (working set vs private bytes; a low resident % means trimmed pages faulting back in),
        page faults/s and GPU utilisation/VRAM. Each call keeps the main-thread sampler (250 Hz) running for
        10 s, so the first call after a pause has few samples; call again a second later. Rates are null on a
        first call."""
        return dumps(await profile_views.top_snapshot(native, symbols(), seconds=seconds, top=top))

    @mcp.tool()
    async def profile_mark(name: str, kind: str = "marker", duration_ms: float = 0.0) -> str:
        """Put a named span ending now on the running profile's timeline (kind: marker, alert, narrate or your
        own word). Ignored when no profile is running."""
        return dumps(await native("Profile.mark", kind=kind, name=name, durationMs=duration_ms))

    @mcp.tool()
    async def profile_trace_summary(path: str | None = None, top: int = 10) -> str:
        """Event-trace summary of a traced profile (the last profile_stop, or path to its output directory or
        raw.json.gz): engine phases in ms per frame (mean, p95, max), the longest frames and which phases filled
        them, draw calls per frame, D3D11 time per render pass and the costliest API calls, OS waits by thread and
        file reads by kind, plus hook install status. Query single events with profile_query on TraceEventsV."""
        target = path or state["raw"]
        if not target:
            return dumps({"error": "no profile yet: run profile_start/profile_stop or pass path"})
        try:
            raw = await asyncio.to_thread(profiling.load_raw, Path(target).expanduser())
            return dumps(profile_trace.summary(raw, top=max(1, min(top, 50))))
        except Exception as error:
            return dumps({"error": f"{type(error).__name__}: {error}"})

    @mcp.tool()
    async def profile_query(sql: str, path: str | None = None, limit: int = 200) -> str:
        """Run one read-only SQL statement (SQLite) against a profile database: path to a profile.sqlite, or by
        default the one the last profile_stop wrote. Tables: Frames (frame, start, end, frame_ms, cpu_ms, gpu_ms,
        hitch, bound), CpuSamples, Callchains (depth 0 = leaf), Symbols, ThreadNames, Markers, GpuRanges,
        Counters, TraceEvents (engine ranges, D3D11 calls/passes, waits, reads), Meta, StringIds; views SymbolsV,
        LeafSamplesV, MarkersV, GpuRangesV, CountersV, ThreadsV, TraceEventsV join names in. Times are ms from the profile start. Writes, ATTACH and PRAGMA are refused; at most `limit`
        rows come back. Example queries are in docs/profiling.md, SQLite export."""
        target = path or state["sqlite"]
        if not target:
            return dumps({"error": "no profile database yet: run profile_start/profile_stop or pass path"})
        try:
            return dumps(await asyncio.to_thread(profile_sqlite.query, target, sql, max(1, min(limit, 5000))))
        except Exception as error:
            return dumps({"error": f"{type(error).__name__}: {error}"})
