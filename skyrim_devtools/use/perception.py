"""Perception tools: what a reference is and what state it is in, and a close-up look at it.

`world_inspect` reads a reference's base form, model, rotation, open/lock state, links and behaviour
graph variables, so an agent can tell a rotating pillar's position or a gate's state without guessing
from pixels. `screen_capture` aims the view at a reference or point, narrows the field of view and
returns a tight full-resolution crop, so carvings, glyphs and notes are readable.
"""

import asyncio
import base64
import json

from mcp.server.fastmcp import Image

MIN_FOV = 5.0
MAX_FOV = 150.0
SETTLE_SECONDS = 0.35
MIN_CROP = 0.06


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def crop_rect(projected, pad=0.15, min_size=MIN_CROP):
    """Normalised crop {x, y, w, h} around a Screen.project reply, padded by `pad` of its size on each
    side and at least `min_size` wide and tall, clamped to the frame. None when nothing to crop to."""
    if not projected or not projected.get("inFront", True):
        return None
    rect = projected.get("rect") or {}
    cx, cy = projected.get("center") or (rect.get("x", 0) + rect.get("w", 0) / 2, rect.get("y", 0) + rect.get("h", 0) / 2)
    w = max(rect.get("w", 0) * (1 + 2 * pad), min_size)
    h = max(rect.get("h", 0) * (1 + 2 * pad), min_size)
    w, h = min(w, 1.0), min(h, 1.0)
    x = min(max(cx - w / 2, 0.0), 1.0 - w)
    y = min(max(cy - h / 2, 0.0), 1.0 - h)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def zoomed_fov(fov, zoom):
    """Field of view for a zoom factor; zoom 1 keeps the current one."""
    if not zoom or zoom <= 1:
        return fov
    return max(MIN_FOV, min(MAX_FOV, fov / zoom))


def image_result(result, extra=None):
    meta = {k: v for k, v in result.items() if k != "data"} if isinstance(result, dict) else {"result": result}
    meta.update(extra or {})
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, str):
        return [dumps(meta)]
    fmt = "jpeg" if "jp" in str(result.get("format", "png")) else "png"
    return [Image(data=base64.b64decode(data), format=fmt), dumps(meta)]


def register(mcp, native, manager):
    @mcp.tool()
    async def world_inspect(ref_id: str, nodes: bool | None = None, graph: bool | None = None,
                            scripts: bool | None = None) -> str:
        """Everything the engine knows about one reference (World.inspect): base form (type, name, editorID),
        model file, rotation angles, open state (open/opening/closed/closing for doors and animated activators),
        locked, linked refs, enable parent, what activates it and what it activates, world bound, line of sight
        from the player, named 3D nodes with their local rotation (nodes, default true), behaviour graph
        variables (graph, default true). scripts=true also lists attached Papyrus scripts with their state and
        variables: that is engine knowledge a player does not have, so say so in a playtest report when used."""
        return dumps(await native("World.inspect", refID=ref_id, nodes=nodes, graph=graph, scripts=scripts))

    @mcp.tool()
    async def world_models(contains: str | None = None, radius: float | None = None, limit: int | None = None) -> str:
        """References around the player by model file name, nearest first (World.models), including statics
        that other tools skip: wall panels, carvings, pillars, gates. contains filters by a case-insensitive
        model-name fragment (e.g. "pillar", "gate", "claw"); radius default 1500, limit default 40."""
        return dumps(await native("World.models", contains=contains, radius=radius, limit=limit))

    @mcp.tool()
    async def screen_capture(ref_id: str | None = None, x: float | None = None, y: float | None = None,
                             z: float | None = None, region: dict | None = None, zoom: float = 1.0,
                             pad: float = 0.15, max_width: int = 1600, aim: bool = True,
                             include_ui: bool = False) -> list:
        """A close, sharp look at one thing. With ref_id (or a world point x,y,z) the view is turned to it
        (aim=false keeps the view), the field of view is narrowed by `zoom` (2 = twice as close, up to 30x,
        restored afterwards), and the frame is cropped tightly around its on-screen bound, at full resolution
        and without the HUD. With region {x, y, w, h} (fractions 0..1 of the frame) that part of the current
        view is cropped instead; include_ui=true keeps menus in the picture (an item shown in the inventory,
        a book page, a map). Returns the image plus where the target was on screen."""
        extra = {}
        crop = None
        restore_fov = None
        target = {k: v for k, v in dict(refID=ref_id, x=x, y=y, z=z).items() if v is not None}
        try:
            if region is not None:
                crop = {k: float(region[k]) for k in ("x", "y", "w", "h")}
            elif target:
                if aim:
                    extra["aim"] = await native("Camera.lookAt", **target)
                if zoom and zoom > 1:
                    state = await native("Camera.state")
                    fov = (state or {}).get("worldFov")
                    if isinstance(fov, (int, float)) and fov > 0:
                        restore_fov = fov
                        await native("Camera.set", fov=zoomed_fov(fov, min(zoom, 30.0)))
                        extra["fov"] = {"from": fov, "to": zoomed_fov(fov, min(zoom, 30.0))}
                await asyncio.sleep(SETTLE_SECONDS)
                projected = await native("Screen.project", **target)
                extra["projected"] = projected
                crop = crop_rect(projected, pad=pad)
                if crop is None:
                    extra["note"] = "the target is behind the camera; returning the whole frame"
            result = await native("Screen.capture", maxWidth=max_width, format="png", includeUI=include_ui, crop=crop)
        finally:
            if restore_fov is not None:
                await native("Camera.set", fov=restore_fov)
        extra["crop"] = crop
        return image_result(result, extra)

    @mcp.tool()
    async def game_setting(name: str, value: float | bool | None = None) -> str:
        """Read an INI or game setting, or set it when `value` is given (Setting.get / Setting.set). INI names
        carry their section: fAutoVanityModeDelay:Camera; game settings are plain (fJumpHeightMin). Changes
        last until the game closes. The idle vanity camera is already switched off for agent sessions."""
        if value is None:
            return dumps(await native("Setting.get", name=name))
        return dumps(await native("Setting.set", name=name, value=value))
