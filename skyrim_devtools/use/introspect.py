"""Introspection tools: Scaleform search, Papyrus classes and calls, form search, idles, camera, map.

Each tool is a thin wrapper over one plugin method; the plugin does the work on the game's threads.
"""

import json


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def alias_ref(quest, alias_id=None, alias_name=None):
    spec = {"quest": quest}
    if alias_id is not None:
        spec["id"] = alias_id
    if alias_name is not None:
        spec["name"] = alias_name
    return spec


def register(mcp, native):
    @mcp.tool()
    async def ui_find(menu: str, name: str | None = None, text: str | None = None, path: str | None = None,
                      depth: int | None = None, limit: int | None = None) -> str:
        """Find values in an open menu's Scaleform tree (UI.find) by member name substring and/or label text
        substring, searching from `path` (default _root) down `depth` levels (default 6, max 12). Returns the
        dotted path of each hit, to pass to ui_get, ui_set or ui_invoke. Works on mod menus such as a SkyUI MCM:
        open it, find the setting by its label, then read or change it by path. Paths are dotted from _root
        (or _global / _levelN); a number indexes an array."""
        return dumps(await native("UI.find", menu=menu, name=name, text=text, path=path, depth=depth, limit=limit))

    @mcp.tool()
    async def papyrus_classes(filter: str | None = None, prefix: str | None = None, offset: int | None = None,
                              limit: int | None = None, detail: bool | None = None) -> str:
        """Papyrus script classes loaded in the VM (Papyrus.classes), sorted by name, filtered by a case-insensitive
        substring (`filter`) or prefix. Pages of `limit` (default 100, max 500) from `offset`; `total` is the
        match count. detail=true adds the parent class and function counts. Classes load on first use, so a mod's
        script may be missing until something touches it."""
        return dumps(await native("Papyrus.classes", filter=filter, prefix=prefix, offset=offset, limit=limit, detail=detail))

    @mcp.tool()
    async def papyrus_functions(class_name: str, filter: str | None = None, kind: str | None = None,
                                offset: int | None = None, limit: int | None = None) -> str:
        """Functions of a Papyrus class and its parents (Papyrus.functions): name, params with types, return type,
        global or member. kind is all, global or member. Use it to learn a function's signature before papyrus_call."""
        return dumps(await native("Papyrus.functions", **{"class": class_name}, filter=filter, kind=kind,
                                  offset=offset, limit=limit))

    @mcp.tool()
    async def papyrus_call(function: str, class_name: str | None = None, args: list | None = None,
                           form: str | None = None, quest: str | None = None, alias_id: int | None = None,
                           alias_name: str | None = None, timeout_ms: int | None = None) -> str:
        """Call a Papyrus function in the running game and return its value (Papyrus.call). A global function needs
        class_name (for example Game, Utility) and no target: papyrus_call("GetPlayer", "Game"). A member function
        takes `form` (hex form id such as "0x14", or an editor id) and optionally class_name (default Actor,
        ObjectReference or Quest by form type; give a script name for an attached script), or `quest` plus
        alias_id/alias_name to call on a quest alias. args follow the declared parameter types; a form argument is
        a hex id or editor id string. Object results come back as {script, formID, name, ...}. Latent functions
        are waited for up to timeout_ms (default 10000, max 120000); on timeout the call keeps running and
        its effects are not undone. See papyrus_functions for signatures."""
        alias = alias_ref(quest, alias_id, alias_name) if quest is not None else None
        return dumps(await native("Papyrus.call", function=function, **{"class": class_name}, args=args or [],
                                  form=form, alias=alias, timeoutMs=timeout_ms))

    @mcp.tool()
    async def forms_find(type: str | None = None, editor_id: str | None = None, name: str | None = None,
                         plugin: str | None = None, offset: int | None = None, limit: int | None = None) -> str:
        """Find loaded forms (Forms.find) by form type name (Weapon, NPC, Quest, Keyword, Idle, Cell, ...), editor ID
        substring, display name substring and/or plugin file substring; at least one is required. Sorted by form id,
        paged by offset/limit (default 50, max 200). Each hit has formID, type, name, editorID, plugin. Most form
        types do not keep editor IDs at runtime, so match those by name or plugin."""
        return dumps(await native("Forms.find", type=type, editorID=editor_id, name=name, plugin=plugin,
                                  offset=offset, limit=limit))

    @mcp.tool()
    async def forms_info(form_id: str | None = None, editor_id: str | None = None) -> str:
        """Metadata of one form (Forms.info): formID, local id, type, name, editorID, defining plugin and every
        overriding plugin, keywords, and for references the base object, position and cell."""
        return dumps(await native("Forms.info", formID=form_id, editorID=editor_id))

    @mcp.tool()
    async def idles_list(ref: str, filter: str | None = None, plugin: str | None = None, playable: bool | None = None,
                         offset: int | None = None, limit: int | None = None) -> str:
        """Idle animations an NPC can play right now (Idles.list): `ref` is the actor's reference id (hex, from
        world_nearby), loaded in the world. playable (default true) keeps only idles whose conditions pass for that
        actor. filter matches editor ID or animation event."""
        return dumps(await native("Idles.list", ref=ref, filter=filter, plugin=plugin, playable=playable,
                                  offset=offset, limit=limit))

    @mcp.tool()
    async def idles_play(ref: str, idle: str) -> str:
        """Make an actor play an idle (Idles.play): idle is a form id or editor id. Returns accepted=false when the
        actor refused it (conditions, furniture, combat)."""
        return dumps(await native("Idles.play", ref=ref, idle=idle))

    @mcp.tool()
    async def idles_stop(ref: str) -> str:
        """Return an actor to its default idle (Idles.stop)."""
        return dumps(await native("Idles.stop", ref=ref))

    @mcp.tool()
    async def camera_pose() -> str:
        """Camera pose and point of view (Camera.pose): mode, pov (first/third/other), position, yaw/pitch in
        degrees, FOV, whether the free camera is on and whether the idle orbit camera may start."""
        return dumps(await native("Camera.pose"))

    @mcp.tool()
    async def camera_mode(mode: str) -> str:
        """Switch the camera to first, third, vanity (the orbit camera) or free (Camera.mode); returns the pose."""
        return dumps(await native("Camera.mode", mode=mode))

    @mcp.tool()
    async def camera_free(on: bool, pos: list | None = None, yaw_deg: float | None = None, pitch_deg: float | None = None,
                          fov: float | None = None, freeze_time: bool | None = None) -> str:
        """Free camera on/off (Camera.free). With on=true and pos [x,y,z], yaw_deg (clockwise from north) and
        pitch_deg (positive looks down) it flies the camera there; call again with on=true to move it. The player
        keeps standing where they were. freeze_time=true pauses the world while it is on. Turn it off before
        playing on: the player cannot be controlled from the free camera."""
        return dumps(await native("Camera.free", on=on, pos=pos, yawDeg=yaw_deg, pitchDeg=pitch_deg, fov=fov,
                                  freezeTime=freeze_time))

    @mcp.tool()
    async def camera_auto_vanity(allow: bool | None = None) -> str:
        """Allow or forbid the idle orbit camera that starts after a pause without input (Camera.autoVanity).
        allow=false stops it and leaves it if it is running; omit allow to read the setting."""
        return dumps(await native("Camera.autoVanity", allow=allow))

    @mcp.tool()
    async def map_waypoint() -> str:
        """The custom map waypoint (Map.getWaypoint): {set, pos, world, distance}."""
        return dumps(await native("Map.getWaypoint"))

    @mcp.tool()
    async def map_set_waypoint(x: float, y: float, z: float | None = None) -> str:
        """Place the custom waypoint at a world position (Map.setWaypoint). The map must be open (open_menu
        MapMenu, wait a moment): the engine places the marker through the map menu, as a click does."""
        return dumps(await native("Map.setWaypoint", x=x, y=y, z=z))

    @mcp.tool()
    async def map_clear_waypoint() -> str:
        """Remove the custom waypoint (Map.clearWaypoint)."""
        return dumps(await native("Map.clearWaypoint"))

    @mcp.tool()
    async def map_markers(contains: str | None = None, visible: bool | None = None, can_travel: bool | None = None,
                          offset: int | None = None, limit: int | None = None) -> str:
        """Map markers of locations (Map.markers) with what the player's map shows: visible (discovered) and canTravel
        (fast travel allowed), position, worldspace, distance. Filter by name substring, visible, can_travel."""
        return dumps(await native("Map.markers", contains=contains, visible=visible, canTravel=can_travel,
                                  offset=offset, limit=limit))
