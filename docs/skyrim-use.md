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
| State (newer plugin builds) | `player_state`, `player_inventory`, `world_nearby`, `world_crosshair`, `quest_get`, `quest_find`, `quest_active`, `quest_targets {quest}`, `activate`, `message_box`, `message_box_select`, `dialogue_state`, `dialogue_select`, `dialogue_wait`, `dialogue_advance`, `dialogue_close`, `ui_list_items`, `ui_select_item`, `ui_dump`, `ui_get`, `ui_set`, `ui_invoke`, `ui_list`, `dialogue_start`, `screenshot` (image), `perf_frame_stats`, `perf_hitches`, `perf_check`, `papyrus_log` |
| Perceive | `world_inspect {ref_id, nodes, graph, scripts}`, `world_models {contains, radius, limit}`, `screen_capture {ref_id or x,y,z or region, zoom, pad, include_ui}` (image), `game_setting {name, value?}` |
| Move and camera | `walk {direction, duration_ms or distance, run?, sprint?}`, `turn_to {heading_deg or x,y or ref_id}`, `jump`, `sneak {on}`, `camera_state`, `camera_set {mode, yaw_deg, pitch_deg, zoom, fov}`, `camera_look_at {ref_id or x,y,z}` |
| Combat | `combat_state {radius}`, `combat_target`, `combat_face`, `combat_draw`, `combat_equip {name, hand}`, `combat_unequip`, `combat_attack {kind=light\|power, hand}`, `combat_block`, `combat_cast`, `combat_drink {kind}`, `combat_shoot {target, shots, draw_s, part, reposition, weapon}`, `combat_fight {style, rounds, drink_below}` (a bounded loop over the primitives), `player_equipped`, `player_spells` |
| Navigate | `nav_nearby {radius, kind}`, `nav_places {contains}`, `nav_goto {goal, mode=input\|ai, timeout_s, on_combat, on_dialogue, on_message_box, on_menu, invulnerable, via, route, activate}`, `nav_plan {ref_id \| x, y, z, route}`, `nav_status`, `nav_wait {timeout_s}`, `nav_cancel` |
| Record | `record_start {hz, fields, name}`, `record_stop`, `record_fetch {name, dest_dir?, plot}`, `trajectory_plot {file, color_by, route}`, `record_replay_camera {name, speed}` |
| Alerts | `agent_alerts {history, limit, dismiss, dismiss_seconds, configure}` (every other reply also carries the active alerts, see Alerts) |
| Generic | `rpc_methods`, `rpc_call {method, params}` |

`game_start` and `game_status` report `plugin` {buildId, missingMethods, stale} and, when the plugin DLL on the host is older than this client, a top-level `warning`; redeploy with `tools/pc-build --deploy` rather than debugging tools that silently do nothing.

The state tools wrap native methods that older plugin builds lack. They answer "method X is not available in
this plugin build" instead of failing obscurely, and `rpc_call` reaches any method before a wrapper exists.

`wait_for` blocks until all given conditions hold: `menu_open`, `menu_closed`, `playable`, `event_type`
(a buffered, not yet drained event; it does not consume it) and a generic probe `method` + `path` + `equals`,
for example `method=Player.state path=location.cell equals=WhiterunBanneredMare`.

**Stuck handling.** Every movement path (`walk`, `nav_goto` in both modes, combat approach) runs the
same native detector: no displacement while moving input is held, a route whose distance to the goal
stops shrinking, ping-pong between spots and a door that keeps being re-entered. It steps back and
strafes, jumps, replans around the spot (blocking a looped door), and then stops with `stuck` within
about 15 s: `walk` returns `{"stuck": true, "error": "stuck", reason, pos, facingDeg, blockers,
screenshotHint}` and `nav_wait` returns `"error": "stuck"` with the same report under `stuck`; take a
`screenshot` to see the blocker. Tune it per call with the `stuck` argument (`false` disables;
`{"distance": 40, "windowS": 3, "maxFailures": 3, "totalS": 20, "progressWindowS": 10}`).
`moveStuck` / `moveUnstuck` events land in the timeline JSONL. `walk` also has a client-side backstop:
if the player covers under 40 units in 20 s the walk is stopped and reported `reason: hardCap`.
Details: docs/protocol.md, "Stuck detector".

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

## Ranged combat

`combat_shoot {target?, shots=3, draw_s?, part=torso|head, reposition=false, max_repositions=2, weapon=bow|spell}`
equips the best bow and arrows (or reports `{"error":"missing","missing":["bow"|"arrows"|"bolts"]}`) and shoots. Why
the naive loop fails and what the tool does about it:

- Short draws give weak, short arrows. Every draw is held for `draw_s` seconds, by default `max(1.5, 1.1 / bow speed + 0.5)`,
  a little longer than a full draw; each shot reports the `heldS` it actually held.
- Aim at the target, not at what is in front of it. The aim point is the target's torso (or head), corrected for arrow
  drop and for movement (the target's velocity is measured over 0.12 s and led by flight time). Arrow speed and gravity come
  from the ammunition's projectile form, then from the first arrow's observed flight (`Ranged.arrows`); the angle between
  the commanded and the actual launch is learnt the same way. A miss reads the arrow's path at the target's range and
  trims the next aim (`missVertical` / `missLateral` in the shot detail).
- A blocked line is detected before drawing. An engine ray from the eye to the target's torso that meets geometry (a
  bridge deck, a wall) returns `noLineOfSight` with the blocker (`hitDistance`, `hitLayer`) and `suggestedPosition`
  `{x,y,z,rangeToTarget}`: points on rings up to 600 units around the player, kept when their own ray is clear and the navmesh
  reaches them, ranked by walking distance and by how far the range lies outside 600-1500. `reposition=true` walks there
  with `Nav.goto` and shoots; the line is re-checked before every shot.
- Hits are verified by the target's health change; `shots`, `hits`, `hitRate`, `damage`, `targetDead`, `repositions` and a
  per-shot `detail` come back. `weapon: "spell"` does the same for a projectile spell in the right hand (a fire bolt, not a
  concentration spell): same ray, ballistic aim with the spell projectile's speed and gravity, a short press instead of a draw.

`combat_fight` switches by itself: when the nearest target cannot be walked to (`Nav.plan` finds no complete path, as for an archer
on a bridge above) it shoots if a bow with arrows is carried, otherwise it ends with outcome `unreachable` and an `advice` line.
The check repeats every 4 rounds. `style="ranged"` shoots instead of walking up.

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

## Mod testing

`dialogue_topics`, `dialogue_explore`, `coverage_start` and `coverage_report` check what a mod's dialogue, quests
and scenes do in a play session; see [mod-testing.md](mod-testing.md).

## Alerts

Alerts tell you when you are wasting time. Every tool reply carries the active ones as a short `alerts` field
(`[{kind, severity, since, hint}]`; a JSON reply gets the key, a plain-text reply an `alerts: [...]` line, an
image reply an extra text part). The field is absent when nothing is active. `agent_alerts` returns them in full
(`sinceMs`, `ageS`, `detail`), with `history=true` the last raised and cleared entries, the plugin's per-tick
`cost`, and the current thresholds. `agent_alerts {dismiss: kind}` hides one you have dealt with for
`dismiss_seconds` (default 300). Each raise and clear is also a `{"t":"alert"}` line in the session timeline, and
the showcase composer shows the active kinds in the status panel.

| Kind | Raised when (default) | What to try |
|---|---|---|
| `questStalled` | the tracked quest's stage and objectives are unchanged for `questStalledMin` (8 min) | re-read `quest_targets`, try another approach |
| `actionLoop` | the same tool with the same arguments gave the same result `actionLoopCount` (3) times within `actionLoopWindowS` (120 s); waiting and status tools are exempt | change the approach |
| `combatStalemate` | in combat `combatStalemateS` (20 s) with neither the player's nor any hostile's health changing | reposition, ranged attack, leave |
| `deathLoop` | `deathCount` (2) deaths within `deathWindowMin` (10 min), or that many within `deathNearDist` (1500 units) of one spot | kit up, level, avoid, or the invulnerable fallback |
| `fellOrOutOfBounds` | falling `fallTimeS` (3 s), `fallDrop` (2000 units) below the last ground, or swimming `swimStallS` (30 s) without getting `swimProgressDist` (600) away | `player.moveto`, `coc`, or reload |
| `blindCamera` | the camera pitch is beyond `pitchLimitDeg` (70) for `pitchBlindS` (15 s), or a `screenshot` is mostly black (`blindMeanLuma` 12) or one colour (`blindDominantShare` 0.92) | look level, check for a menu or fade |
| `menuLeftOpen` | a non-HUD menu is open `menuOpenS` (30 s) with no input request | close it |
| `paused` | the game is paused with no menu open, or (windowed runs) not the foreground window, for `notFocusedS` (5 s) | close the menu or refocus |
| `lowFps` | fps under `lowFpsBelow` (30) for `lowFpsS` (10 s), or one frame over `hitchMs` (1000 ms; held 30 s) | pause actions, `perf_hitches` |
| `overEncumbered` | carried weight above the carry limit | drop or store items |
| `crime` | a bounty gained, stolen goods value rising, or a hostile guard (held 60 s) | pay, jail or avoid |
| `targetDead`, `targetUnreachable` | a tracked quest's target actor is dead (the detail says if it is essential), or is disabled or in no cell; checked every `targetCheckS` (10 s) | re-check the quest, reload, or another route |
| `lowHealthNoHeal` | health under `lowHealthPct` (30) with no restoring potion and no healing spell | retreat, rest, console heal |
| `moveStuck` | the movement stuck detector's `moveStuck` event, until `moveUnstuck` | another route, open the door |

Change thresholds with `agent_alerts {configure: {"thresholds": {"questStalledMin": 3}, "disable": ["crime"]}}`;
the plugin owns the native keys (`Alerts.configure`), the MCP server the `actionLoop*` and `blind*` ones. Alerts
are not shown before a save is loaded, and a save load resets every timer.

Cost: a poller thread asks the game thread for one sample about every `pollMs` (500 ms), and all detectors run on
the poller thread. The game-thread part is a handful of reads (the quest and target checks every 5 s and 10 s,
the potion scan only while health is low). `agent_alerts` reports it as `cost`: `mainMeanUs` and `mainMaxUs` are the
game-thread time per sample, `pollerMeanUs` the detector time; the sample also appears in `Perf.hitches`
`slowCalls` as `Alerts.tick` if it ever exceeds 5 ms.

## Performance

Call `perf_check` first in every playtest, once the world is loaded and before any other tool. It waits 10 s,
then reports fps, p50/p99 frame time and the worst hitches, and warns when fps is under 30 or p99 is over
100 ms. On a warning do not play on: read `perf_hitches` (each hitch with the frame thread's CPU time versus
blocked time, process page faults and disk traffic, and the plugin's own main-thread calls that overlapped it),
then check the host (`windows/perf-watch.ps1` samples memory, paging, disk queue, CPU per process and GPU load
once a second into a CSV), and restart the game if nothing explains it.

* `Perf.hitches` logs every frame over 100 ms (`Perf.configure {hitchLogMs}`); `slowCalls` tallies each handler
  that held the game thread over 5 ms (also written to the plugin log from 20 ms).
* `tools/perf_session.py` is the A/B harness: one visible game session, phases of idle, walking, polling,
  scanning and a Riverwood navigation loop with the desktop capture, one reading per phase.
* Long visible sessions have hung (the game's thread count grows by about 2 per second until the main thread waits
  forever or `XAudio2_7.dll` crashes) on a PC with no audio output device; see
  [the Embershard playtest](playtests/embershard-mine.md). `disableAudio: true` in the host's `config.json` writes
  `bEnableAudio=0` for visible runs and slows the growth. Restart the game after about 30 minutes, and watch
  `Get-Process SkyrimSE` thread count next to `perf_check`.
* A visible game runs borderless-windowed at the profile's resolution (`bFull Screen=0`): exclusive fullscreen
  drew the picture in a corner of the monitor and made the desktop capture black. `Window.info` reports the
  window, client, monitor, swap chain size and DPI awareness, to check the game fills the screen.

## Long sessions

Restart the game about every 30 minutes (`game_stop`, then `game_start`, then load the save again) until the cause of
the thread growth is fixed: the game's thread count rises by roughly two per second during play and the main thread
hung at a few thousand threads in the long playtests. `game_stop` leaves the host ready for the next `game_start`
(it ends the scheduled task and removes the stale lease marker), and `game_start` clears the same leftovers itself
when no game is running (`staleCleared` in its reply says what it removed).

`game_status` carries a `health` object read from the operating system, so it also answers when the game is hung:

* `threads`, `handles`: the leading indicator; a healthy start is about 100 to 250 threads. Watch the trend, not one value.
* `privateMB`, `workingSetMB`: memory of the game process.
* `cpuCores`: CPU used over the last half second, in cores (1.0 is one fully busy core); a hung game with a blocked
  main thread still burns several.
* `responding`: false when the game window stopped answering messages; `uptimeS`: age of the process.
* `healthWarnings` (and `warning`): `threadCount` at 1500 threads or more, `threadGrowth` when the count rose by 60 or
  more per minute over at least a minute, `notResponding`, and an informational `longSession` after 30 minutes. The
  limits are config keys `healthThreadWarn`, `healthThreadGrowthPerMin`, `healthGrowthWindowS`, `healthGrowthMinSpanS`
  and `healthRestartAfterMin`. The MCP link samples health once a minute in the background so the growth rate is
  known even when nobody calls `game_status`. Each warning has a stable `code` so the alerts layer can raise it as an
  alert (the single integration point is `GameManager.health_watch.warnings`).

`combat_fight` warns at its start (`warnings`, plus a `warning` event for `events_drain`) when the inventory holds no
healing potion and the player knows no healing spell. `windows/perf-watch.ps1 -Out FILE -Detach` starts the host
sampler detached from the SSH session (once a second, with the game's thread count in `gameThreads`); `-Stop` ends it.

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
