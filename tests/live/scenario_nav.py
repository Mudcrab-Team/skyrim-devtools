"""Session-style live case for scenario_speed's `nav` check (the harness already has the game loaded)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenario_speed as base  # noqa: E402


async def run(ctx):
    tool = base.make_tool(base.build_server(ctx.manager, base.Recorder(tempfile.mkdtemp(prefix="scenario-live-"))))
    details = {}
    await base.run_cases(ctx.manager, tool, ["nav"], details)
    return base.verdict(details)
