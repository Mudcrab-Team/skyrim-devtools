# skyrim-use: playtest Skyrim from Claude

`skyrim-use` is an MCP server (stdio) that drives the DevTools plugin described in
[protocol.md](protocol.md). It starts the game on the Windows host, keeps one session alive across tool
calls, and exposes menus, console, input, state queries and screenshots as tools an LLM can call.

## Install and register

~~~sh
pip install -e '.[video]'   # installs skyrim-use, skyrim-devtools-smoke and skyrim-devtools-video (Python 3.13)
claude mcp add skyrim-use -- skyrim-use --config /path/to/devtools.json
~~~

The config is the same JSON the smoke test takes:

~~~json
{ "sshDestination": "skyrim-pc", "sshConfig": "~/.ssh/skyrim-pc.conf", "localPort": 48681 }
~~~

`--config` may be replaced by the `SKYRIM_USE_CONFIG` environment variable. Any MCP client works; the
server speaks plain stdio. When the client exits the server stops the game, so a session never leaks the host.

## How a session behaves

* `game_start` runs the host's start, waits for the plugin, opens the SSH tunnel, subscribes to events and
  optionally loads a save. It takes 15-90 s. It is idempotent while a game is running.
* A keepalive ping every 15 s watches the link. When the link drops, the server asks the host whether the game
  is alive. If so it reconnects (and resubscribes); if not it reports phase `crashed` with the host's phase,
  reason and exit code, and every tool then says so until `game_start` relaunches.
* The game ignores all input while the load fade is on screen, and a menu ignores input for about 0.3 s after it
  opens. `wait_for(playable=true)` accounts for the fade; after `open_menu` wait for the menu, then give it a beat.
* Do not close the socket without stopping through the host: the game exits on its own, and the host records
  that as `unrequested-exit`. `game_stop(grace_seconds=12)` closes open gameplay menus (quitting with the inventory open crashes the engine on one of its worker threads), asks the game to quit, kills it after the grace period, and reports `cleanExit` and `path` (`graceful`, `killed-after-Ns`, `closing-crashed`). A crash comes with `crash.log`: the crash logger's exception and top frames by module and Address Library id.
* The engine sometimes dies about a second after launch (null call on its audio thread, before any plugin code runs; about 1 start in 15). `game_start` repeats such a start up to twice and lists the crashes under `startRetries`.

## Tools

| Group | Tools |
| --- | --- |
| Lifecycle | `game_start {headless=true, save?}`, `game_stop`, `game_status`, `game_save {name, keep=true}` (the host wipes its Saves folder on every launch; `game_save` also copies the file to the fixtures folder so `game_start {save: name}` finds it later) |
| Observe | `snapshot`, `list_menus {open_only}`, `events_list`, `events_subscribe`, `events_drain {types?, limit}`, `wait_for` |
| Act | `open_menu`, `close_menu`, `console {command, target}`, `press_key`, `press_control`, `mouse`, `release_all_input`, `look {yaw_deg, pitch_deg}` |
| Saves and world | `list_saves`, `load_save`, `reload`, `center_on_cell` |
| State (newer plugin builds) | `player_state`, `player_inventory`, `world_nearby`, `world_crosshair`, `quest_get`, `quest_find`, `quest_active`, `activate`, `message_box`, `message_box_select`, `dialogue_state`, `dialogue_select`, `dialogue_wait`, `dialogue_advance`, `dialogue_close`, `ui_list_items`, `ui_select_item`, `ui_dump`, `ui_get`, `ui_set`, `ui_invoke`, `ui_list`, `dialogue_start`, `screenshot` (image), `perf_frame_stats`, `papyrus_log` |
| Perceive | `world_inspect {ref_id, nodes, graph, scripts}`, `world_models {contains, radius, limit}`, `screen_capture {ref_id or x,y,z or region, zoom, pad, include_ui}` (image), `game_setting {name, value?}` |
| Move and camera | `walk {direction, duration_ms or distance, run?, sprint?}`, `turn_to {heading_deg or x,y or ref_id}`, `jump`, `sneak {on}`, `camera_state`, `camera_set {mode, yaw_deg, pitch_deg, zoom, fov}`, `camera_look_at {ref_id or x,y,z}` |
| Combat | `combat_state {radius}`, `combat_target`, `combat_face`, `combat_draw`, `combat_equip {name, hand}`, `combat_unequip`, `combat_attack {kind=light\|power, hand}`, `combat_block`, `combat_cast`, `combat_drink {kind}`, `combat_fight {style, rounds, drink_below}` (a bounded loop over the primitives), `player_equipped`, `player_spells` |
| Navigate | `nav_nearby {radius, kind}`, `nav_places {contains}`, `nav_goto {goal, mode=input\|ai, timeout_s, on_combat, on_dialogue, on_message_box, on_menu, invulnerable, via, route}`, `nav_plan {ref_id \| x, y, z, route}`, `nav_status`, `nav_wait {timeout_s}`, `nav_cancel` |
| Record | `record_start {hz, fields, name}`, `record_stop`, `record_fetch {name, dest_dir?, plot}`, `trajectory_plot {file, color_by, route}`, `record_replay_camera {name, speed}` |
| Generic | `rpc_methods`, `rpc_call {method, params}` |

`game_start` and `game_status` report `plugin` {buildId, missingMethods, stale} and, when the plugin DLL on the host is older than this client, a top-level `warning`; redeploy with `tools/pc-build --deploy` rather than debugging tools that silently do nothing.

The state tools wrap native methods that older plugin builds lack. They answer "method X is not available in
this plugin build" instead of failing obscurely, and `rpc_call` reaches any method before a wrapper exists.

`wait_for` blocks until all given conditions hold: `menu_open`, `menu_closed`, `playable`, `event_type`
(a buffered, not yet drained event; it does not consume it) and a generic probe `method` + `path` + `equals`,
for example `method=Player.state path=location.cell equals=WhiterunBanneredMare`.

`nav_goto` returns at once; follow it with `nav_wait` (blocks up to 300 s per call) or `wait_for
event_type=navArrived`. Use `mode=ai` for long overland trips (the engine pathfinder handles cell
streaming and roads) and `mode=input` for short, precise walks that behave like a player pressing
keys. With a worldspace graph built by `skyrim-devtools-navgraph build` (see docs/protocol.md,
"Global routes"), `mode=input` also crosses the map: far goals follow a gate-to-gate route and
`nav_plan route=global` shows it. `record_fetch` copies a recording over scp using the host config (`recordingsDir` there, or
`./recordings`) and writes an SVG next to it; the same plot is available offline as
`skyrim-devtools-trajectory plot <file> -o out.svg [--color speed|frame]`. A long trip can be
sped up with `console sgtm 4` (and `sgtm 1` afterwards); see docs/protocol.md for the navigation
modes and their limits.

## Perception

A player reads a puzzle with their eyes; an agent with a 1600-pixel screenshot of a dark room cannot. The
perception tools close that gap without handing over engine-only answers:

* `world_models contains=pillar` lists references by model file, statics included (carvings, pillars, gates,
  webs, word walls), nearest first. `world_nearby` skips statics, so this is how to find puzzle parts.
* `world_inspect ref_id` returns base form, model, angles, open state (doors and animated activators), locked,
  linked refs, enable parent, what activates it and what it activates, the world bound, line of sight, named 3D
  nodes with their local rotation and behaviour-graph variables. A rotating pillar's node rotation tells which
  face points out; a chain's "activates" names the portcullis it opens. `scripts=true` lists Papyrus scripts
  and their variables: say so in a playtest report, since a player cannot see that.
* `screen_capture ref_id zoom=4` turns the view to the target, narrows the field of view (down to 5 degrees,
  restored afterwards), and returns a full-resolution crop around its on-screen bound without the HUD.
  `region {x,y,w,h}` crops the current view instead; `include_ui=true` keeps menus, for example to read the
  carving on an item rotated in the inventory.
* `game_setting` reads or sets INI and game settings (`fAutoVanityModeDelay:Camera`). Agent sessions switch the
  idle vanity camera off at start, since an agent idles between calls and the orbiting camera spoils aiming,
  screenshots and footage.
* When `nav_goto` fails at a dead end, or `nav_plan` returns an incomplete path, the reply carries `blockers`:
  the stop point, the gap to the goal, the doors, gates, webs and activators near the stop with their state,
  and a `likely` cause. It is a heuristic: in The Golden Claw run it once blamed a web 300 units away while
  the real blocker was a different web door, so check `likely` with `world_models` and a capture.

`events_drain` returns compact `{type, seq, epoch, data}` records since the previous drain. After
`game_start` every declared event is buffered except `console` (the `console` tool already returns its lines).

## Dialogue

A topic click starts spoken lines (voice takes real time) and the new topic list appears only after the last
one. Loop: `dialogue_start` -> `dialogue_wait` (greeting and topics) -> `dialogue_select` -> `dialogue_wait` ->
... -> `dialogue_close`. `dialogue_wait skip=true` skips each line as soon as its text was captured and is the
fast path (and the one that works with muted audio). A trainer, merchant or gift topic opens another menu over
the dialogue; `dialogue_wait` then answers `submenu` and the conversation stays frozen until that menu is closed
so act in it first and leave it with the exit key (`press_control Cancel`) or `dialogue_close`; hiding a training menu with `close_menu` was followed by a frozen game within two minutes in two of two runs, so `open_menu` now refuses trainer, barter, gift, container, lockpicking, crafting, dialogue and book menus (pass `force` to override) and `close_menu` leaves them with the Cancel control. Muted (headless) audio does not stall a conversation, but every line still lasts as long as its voice, so use `skip`. The plugin turns dialogue subtitles on so NPC lines can be read.

## Showcase video

For a non-technical audience: game footage beside an "agent view" panel (tool calls, status, captions).

1. `game_start headless=false`, then `capture_start {fps=30, width=1920}`. The host records the desktop with
   ffmpeg (winget `Gyan.FFmpeg`, user scope) from an interactive scheduled task, GDI capture first and
   desktop duplication as fallback, GPU H.264 (AMF) with libx264 fallback, video only. It re-focuses the game
   afterwards (an unfocused windowed game stops answering). Measured cost on the test PC: no change in
   `Perf.frameStats` (59.95 fps before, 59.94 during) in a short test. In a 110-minute session the game sank to
   4-15 fps from multi-second hitches (median frame still 16.7 ms), and stopping the capture did not help, so
   the capture is not the main suspect for long-session hitches. Install the task once with
   `host.ps1 install-capture-task`.
2. Every tool call is appended to a timeline JSONL (`$SKYRIM_USE_TIMELINE_DIR`, default
   `~/.skyrim-use/timelines/<UTC stamp>.jsonl`): name, args that differ from defaults, start/end epoch ms,
   a short result summary, errors. `narrate {text}` adds an on-screen caption, `track_quest {editor_id}`
   chooses the quest shown in the status block, and a sampler adds a `status` line every 4 s when the
   state changed (cell, position, health, quest stage and objective, nav).
3. `capture_stop` stops the recording and copies the MP4 next to the timeline (about 40 MB per 30 s at 8 Mbit/s;
   the copy dominates the call over a slow link). The footage start (UTC epoch ms, within about 0.2 s) is in
   the timeline and in `<stamp>.capture.json`.
4. Compose on the Mac (installed by the command above; needs ffmpeg on PATH):

~~~sh
skyrim-devtools-video compose --footage x.mp4 --timeline x.jsonl -o out.mp4 \
    --title "An AI agent plays Skyrim" --speedup-idle 4     # --dry-run prints the speed plan
~~~

Layout is 1920x1080: footage left, caption bar below it, agent panel right, intro and outro cards. Gaps longer
than `--idle-threshold` (3 s) with no tool call or caption run at `--speedup-idle` (never longer than
`--max-idle` seconds in the result); the middle of waits and walking tools (`wait_for`, `load_save`, `nav_*`,
...) is sped up while their start and end stay at 1x; dialogue tools stay at 1x with padding.

A long session (an hour or more of footage) is better cut than sped up. `--highlights` keeps only the
stretches around `narrate` markers (`--highlight-before` 4 s ahead of each marker, `--highlight-after` 6 s past
its caption), `--keep START-END` (seconds or `M:SS`, repeatable) adds stretches by hand, and `--skip START-END`
cuts stretches out of either, such as a trip that was later replayed from a save. Within the kept stretches
the speed plan above still applies, jumps get a short fade, and `--target-length` speeds the whole cut up by
one factor (at most 2x) to hit a length. So narrate at the moments a viewer should see: the tool picks the
cut from them.

When the footage stays on the host (`capture_stop fetch=false`, for example a 3 GB recording and a full Mac
disk), build the inputs locally and run ffmpeg on the host:

~~~sh
skyrim-devtools-video compose --footage 'C:/path/on/host/capture.mp4' --timeline x.jsonl -o cut.mp4 \
    --highlights --target-length 285 --emit-dir work/     # panel stills, cards, graph, command.json
# copy work/ to the host, run the argv in command.json from inside it, copy cut.mp4 back
~~~

`--emit-dir` takes the footage length from the capture stop record in the timeline (or `--duration`).
The composer needs ffmpeg 7 or newer.

## Example transcript

~~~text
game_start {headless: true}
  {"phase":"running","mode":"headless","info":{"protocol":2,"dataLoaded":true,"loadEpoch":0},"methodCount":26}
load_save {name: "Fixture"}                      {"loadEpoch":1,"name":"Fixture","success":true}
wait_for {playable: true, timeout_s: 60}         {"ok":true,"waitedMs":3386,"state":{"snapshot":{"playable":true,...}}}
console {command: "GetAV Health", target: "player"}   GetActorValue: Health >> 100.00
press_control {name: "Tween Menu"}
wait_for {menu_open: "TweenMenu", timeout_s: 5}  {"ok":true,"waitedMs":468,...}
press_control {name: "Tween Menu"}               (closes it)
open_menu {name: "InventoryMenu"}                {"changed":true,"menu":"InventoryMenu","open":true}
list_menus {}                                    ["Cursor Menu","HUD Menu","InventoryMenu"]
events_drain {limit: 5}                          {"events":[{"type":"menuOpenClose","seq":1,"epoch":1,"data":{...}}],...}
screenshot {}                                    (image)
game_stop {}                                     {"phase":"stopped","cleanExit":true}
~~~

## Tests

`python3.13 -m unittest discover -s tests` runs the server against a fake host and plugin; no game is needed.
