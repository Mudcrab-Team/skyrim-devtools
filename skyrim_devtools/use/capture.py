"""Game footage capture on the Windows host and the MCP tools around it.

The capture runs as an interactive scheduled task on the host (desktop duplication needs the real desktop
session), is controlled through host.ps1, and its MP4 is fetched over scp when stopped.
"""

import asyncio
import base64
import json
from pathlib import Path
import re
import subprocess

from .manager import GameError
from .timeline import now_ms

SESSION_RE = re.compile("[a-f0-9]{32}")


class CaptureHost:
    """Runs host.ps1 capture actions and copies files back, reusing the WindowsHost SSH settings."""

    def __init__(self, host):
        self.host = host

    async def action(self, action, session_id, extra="", timeout=60):
        if action not in {"capture-start", "capture-stop", "capture-status"} or not SESSION_RE.fullmatch(session_id):
            raise ValueError("Invalid capture operation")
        script = rf"& (Join-Path $env:USERPROFILE '.local\skyrim-devtools\host.ps1') {action} -Session {session_id} {extra}"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        process = await asyncio.create_subprocess_exec(
            *self.host.ssh, "-T", self.host.destination, "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "RemoteSigned", "-EncodedCommand", encoded,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            async with asyncio.timeout(timeout):
                data, err = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GameError(f"host {action} timed out")
        if process.returncode:
            raise GameError(f"host {action} failed: {err.decode(errors='replace').strip()[:300]}")
        try:
            return json.loads(data)
        except ValueError as error:
            raise GameError(f"invalid response from host {action}") from error

    async def fetch(self, remote_path, local_path):
        scp = [part for part in self.host.ssh if part != "ssh"]
        # scp takes the same -o/-F options as ssh; ConnectTimeout and BatchMode carry over
        remote = remote_path.replace("\\", "/")
        process = await asyncio.create_subprocess_exec(
            "scp", "-q", *scp, f"{self.host.destination}:{remote}", str(local_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, err = await process.communicate()
        if process.returncode:
            raise GameError(f"could not fetch the footage: {err.decode(errors='replace').strip()[:300]}")


def register(mcp, manager, recorder, host=None):
    capture_host = CaptureHost(host or manager.host)

    def need_session():
        if not manager.session_id:
            raise GameError("start the game first (game_start): footage is stored with the game session")
        return manager.session_id

    @mcp.tool()
    async def capture_start(fps: int = 30, width: int = 1920) -> str:
        """Start recording the game's desktop as MP4 on the test PC (GPU-encoded, video only). Needs a
        non-headless game (game_start headless=false): headless runs have no visible window. width scales
        the footage (default 1920). The start time is recorded in the session timeline so a showcase video
        can be composed later with skyrim-devtools-video."""
        if manager.mode == "headless":
            raise GameError("capture needs a windowed game: call game_stop, then game_start with headless=false")
        session = need_session()
        state = await capture_host.action("capture-start", session, f"-Fps {int(fps)} -Width {int(width)}", timeout=40)
        if state.get("phase") != "recording":
            raise GameError(f"capture did not start: {state}")
        recorder.capture = {"startEpochMs": state["startEpochMs"], "remote": state["path"], "file": state["file"]}
        recorder.write({"t": "capture", "ms": now_ms(), "action": "start", "startEpochMs": state["startEpochMs"],
                        "file": state["file"], "encoder": state.get("encoder")})
        return json.dumps({"recording": True, "encoder": state.get("encoder"), "fps": state.get("fps"),
                           "startEpochMs": state["startEpochMs"]})

    @mcp.tool()
    async def capture_stop(fetch: bool = True) -> str:
        """Stop the recording and copy the MP4 next to the session timeline on this machine. Returns the
        local path (compose with `skyrim-devtools-video compose --footage <mp4> --timeline <jsonl>`)."""
        session = need_session()
        state = await capture_host.action("capture-stop", session, timeout=60)
        if state.get("phase") != "stopped":
            raise GameError(f"capture did not stop cleanly: {state}")
        local = recorder.directory / f"{recorder.stamp}.mp4"
        if fetch:
            await capture_host.fetch(state["path"], local)
        recorder.write({"t": "capture", "ms": now_ms(), "action": "stop", "startEpochMs": state["startEpochMs"],
                        "stopEpochMs": state["stopEpochMs"], "file": state["file"]})
        recorder.capture = None
        meta = {"startEpochMs": state["startEpochMs"], "stopEpochMs": state["stopEpochMs"], "encoder": state.get("encoder")}
        if fetch:
            local.with_suffix(".capture.json").write_text(json.dumps(meta))
        return json.dumps({"localPath": str(local) if fetch else None, "remotePath": state["path"],
                           "bytes": state.get("bytes"), "timeline": str(recorder.path), **meta})

    @mcp.tool()
    async def narrate(text: str) -> str:
        """Leave a short caption (one or two sentences, first person) for the showcase video: say WHY you are
        about to do something or what you concluded, e.g. "I'll side with Faendal because he asked first".
        It appears on screen at this moment of the footage. Costs nothing in the game."""
        text = text.strip()
        if not text:
            raise GameError("narrate needs some text")
        recorder.narrate(text)
        return "ok"

    @mcp.tool()
    async def track_quest(editor_id: str | None = None) -> str:
        """Choose the quest (editor id, e.g. MQ101) whose stage and current objective appear in the video's
        status panel; omit to fall back to the first active quest."""
        recorder.track_quest = editor_id
        return json.dumps({"trackedQuest": editor_id})
