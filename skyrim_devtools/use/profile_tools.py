"""Profiler tools: sample the game's threads, then get a symbolised report and Firefox Profiler / flame graph files.

The report names game functions from a local skyrim-research checkout (config "researchCheckout" or the
SKYRIM_DEVTOOLS_RESEARCH variable); without one it falls back to Address Library IDs and sub_<rva>.
"""

import json
from pathlib import Path
import time

from .. import profiling


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def register(mcp, native, manager):
    state = {"symbols": None}

    def symbols():
        if state["symbols"] is None:
            state["symbols"] = profiling.GameSymbols(profiling.research_root(config=manager.config))
        return state["symbols"]

    @mcp.tool()
    async def profile_start(threads: str = "main", hz: float = 1000.0, duration_s: float = 30.0) -> str:
        """Start the sampling CPU profiler (Profile.start). threads: main (the game thread), frame (the thread
        that presents), all, or comma-separated thread ids / name fragments. hz 10-4000 (default 1000);
        duration_s caps the run (max 600). Do the thing to profile, then call profile_stop."""
        return dumps(await native("Profile.start", threads=threads, hz=hz, durationS=duration_s))

    @mcp.tool()
    async def profile_stop(top: int = 10, out_dir: str | None = None) -> str:
        """Stop the profiler and return the report: top functions by self and total time (on-CPU), hottest
        stacks, the module split (game / plugin / skse / gpu-driver / kernel / system / other-dll), what the
        thread waited on, slow frames (>100 ms) with their stacks and wait chain, sampler overhead and the
        share of game samples with a real function name. Also writes raw data, a Firefox Profiler profile
        (profile.json.gz, open at profiler.firefox.com) and flame graph SVGs to out_dir (default
        .local/profiles/<stamp> on this machine) and returns their paths."""
        raw, _ = await profiling.fetch(native)
        profile = profiling.Profile(raw, symbols())
        out = Path(out_dir).expanduser() if out_dir else profiling.default_out()
        paths, summary = profiling.save_outputs(profile, out)
        summary["files"] = paths
        for key in ("main", "all"):
            if key in summary:
                for field in ("topSelf", "topTotal"):
                    summary[key][field] = summary[key][field][:top]
        return dumps(summary)

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
