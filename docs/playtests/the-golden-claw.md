# Playtest: The Golden Claw (Riverwood Trader, Bleak Falls Barrow)

Quest `MS13`. Played as an agent through the skyrim-use tool set only (the MCP tool functions, driven by a
small local daemon that forwards one call per line), windowed fullscreen at the game's native resolution,
offline test profile, Fixture save in Riverwood (level 1, iron axe, iron shield, iron armor, 5 minor healing
potions, 140 gold).

Result: **not completed**. The run reached stage 10 ("Retrieve the Golden Claw") and the inside of Bleak Falls
Barrow past the bandit hall, the lever room and the giant-rat gate, but never reached Arvel the Swift and so never saw
the claw. The way down is closed behind the rotating-pillar puzzle and the agent could not perceive its clue
(friction 5). No cheats were used: no god mode, no teleport, no console quest commands.

| | |
| --- | --- |
| Final stage | 10 of the quest (objective "Retrieve the Golden Claw"); Arvel and the claw not reached |
| Live game time | about 85 min over four sessions (footage 1893 s, 1653 s, about 600 s, about 700 s) |
| Tool calls | about 1040 (about 660 recorded in the video timelines, the rest are polling and screenshots) |
| Deaths / retries | 1 death (attempt 1, autopilot fight policy at the barrow entrance), 1 full replay of the first 15 minutes because saves are wiped (friction 1) |
| Fights | 9 encounters won (a wolf, three bandit pairs counting the replay, three rat groups, a lone bandit), 1 lost |
| Cheats | none |
| PC time | 4 sessions; lock waits of 8, 29, 10 and 10 minutes |

## Decisions (as narrated in the video)

1. **Take the job.** Lucan Valerius has been robbed of a golden dragon claw. The agent asked what happened, then
   offered to get it back. Stage 10 followed about 10 s after the conversation closed (friction 9).
2. **Travel and combat policy.** First attempt: `nav_goto` with `on_combat: "fight"` all the way. A level-1
   character walked into the bandits around the barrow entrance and died. Lesson written into the next caption:
   scout, stop on contact, and choose how to fight. Later legs used `on_combat: "stop"`, then
   waited with the shield up until the enemy was close (`waitclose`), then `combat_fight` (block when the enemy
   winds up, light attacks normally, power attack when stamina allows, health potion below 40 percent).
3. **Gear.** The fixture already carries a shield; `combat_equip` put it in the left hand after every load
   (loads reset it). Armor and axe were kept; the long bow was not used.
4. **Entrance.** The map marker is about 1100 units below the real door and a long slope away. The door was
   found by scanning `world_nearby types=[door]`: an iron door whose destination is "Bleak Falls Temple".
5. **Rats behind a gate.** The fight loop wasted a minute swinging at an iron gate; a lever to the side opens it.
6. **Puzzle.** Three rotating pillars and a lever. The clue panels could not be found by screenshot (friction 5),
   so the agent enumerated pillar combinations while watching `Nav.plan` for a route to Arvel. The sweep was
   invalid the first time (one pillar out of reach, and the agent stood in the dart line), and valid the second
   time but found no opening, so the problem is either the clue or a wrong detector. The session ended there.

## Condensed tool-call log

| # | Calls | Result |
| --- | --- | --- |
| 1 | `game_start headless=false save=Fixture`, `combat_state`, `player_inventory`, `quest_find "Golden Claw"` | MS13 found by name; Fixture is armed |
| 2 | `capture_start`, `track_quest MS13`, `nav_goto {cell: RiverwoodRiverwoodTrader}` | walks through the load door by itself |
| 3 | `dialogue_start`, `dialogue_wait skip`, `dialogue_select 0` x2 | stage 5, then 10 a few seconds later |
| 4 | `nav_goto {place: "Bleak Falls Barrow"} on_combat=fight` | wolf fought, then death at the entrance (retry) |
| 5 | reload, `nav_goto on_combat=stop`, `combat_equip Iron Shield`, `combat_fight` | wolf won in 8 rounds |
| 6 | `nav_wait` x N, `fight.sh` (bursts of `combat_fight` rounds=6) | 2 bandits outside won; a "Health Low" tutorial popup froze one fight (friction 4) |
| 7 | `nav_goto mode=ai {x,y,z}` then `nav_goto {near: door}`, `activate` | inside the barrow |
| 8 | `world_nearby types=[door,activator,furniture]`, `nav_goto {cell: BleakFallsBarrow02}` | gate hall; two more bandits won |
| 9 | `nav_goto {near: lever}`, `activate` lever, `combat_fight` | rats won; Arvel visible 2200 units away, not reachable |
| 10 | `nav_goto {near: Arvel}` mode input and ai | "Dead end: no way closer" at a spider-web wall |
| 11 | `game_save`, `capture_stop`, `game_stop` (x3 sessions) | checkpoints kept in the host fixtures folder |
| 12 | `camera_look_at`, `screenshot` x about 20 | no readable clue (friction 5) |
| 13 | `activate` pillars and lever x 54, `rpc_call Nav.plan` x 54 | no route opened |

Stages observed: 0 -> 5 -> 10. Objective 15 ("Cut Arvel down") and later ones never displayed.

## Combat toolkit added by this run

`Combat.state`, `target`, `face`, `draw`, `equip`, `unequip`, `attack` (light/power), `block`, `cast`, `drink`
(native, `native/combat.cpp`), wrapped as `combat_*` MCP tools plus `combat_fight`, a bounded loop over them
whose decisions live in a pure function (`decide`) with unit tests. Verified live: equip (shield, weapon), draw,
light and power attacks (stamina drops as expected), block, potion use, target and facing, hostile health and
attack phase, 9 won fights. Not exercised live: `Combat.cast` (the fixture has no attack spell) and
`Combat.unequip`.

## Frictions, bugs and missing tools

1. **Saves are wiped on every launch** (high). The host clears the profile's Saves folder at start and restores
   only the fixtures folder, so a checkpoint made in one session is gone in the next; a 15 minute replay was
   needed, and a long quest cannot be split across lock slots. Fixed here: `game_save {name}` saves and copies the
   file into the fixtures folder. Still open: the host could keep saves with a `-KeepSaves` start flag.
2. **Map markers are not entrances, and the input walker traps itself on slopes** (high). `nav_goto place=Bleak Falls
   Barrow` aims at a marker 1100 units below and away from the door and the input mode spent 3 minutes
   on a snowy slope with `stuckCount` 94; `mode=ai` and a door reference fixed it. Suggest: `nav_goto {place}`
   resolves to the location's load door, and the input mode fails fast (not after 94 recoveries) when slope
   or cliff blocks it.
3. **"Dead end" without a reason** (high). Both Arvel attempts ended with "no way closer" at a web wall, with
   no hint what blocks (gate, web, puzzle). Suggest: `Nav.plan` returns the last reachable point and the
   nearest blocking references (gates, webs, activators) around it.
4. **Tutorial popups freeze the game** (high). "Health Low" opens a Tutorial Menu that pauses the world; the
   fight loop kept swinging at nothing for a minute, and `message_box` reports closed because it is not
   a message box. Fixed in `combat_fight` (closes the popup, refuses on other menus). Suggest: a test
   profile with tutorials disabled, and `game_status` listing blocking menus.
5. **No way to perceive puzzle clues** (high). Carved clue panels are high on the wall or on the floor; screenshots at
   eye level, tilted up, and `camera_look_at` found only blank panels and statue heads. `Ref.get` on the pillars
   has no rotation, model or animation state, and `world_nearby` has no static category. Suggest:
   `world_inspect {ref_id | point}` returning model name, rotation, animation state, and a `screenshot
   {crop, zoom}` option, plus `world_nearby types=[static]` with model path names (glyph, clue).
6. **The vanity camera takes over after idle** (medium). After a minute without input the camera switched to
   `autoVanity` and screenshots turned into a third-person orbit. Suggest: keep the camera in first person
   (disable the vanity delay in the test profile) or report the mode in `snapshot`.
7. **`Nav.goto` keeps being interrupted by combat with no enemy** (medium). The player stays `inCombat` for a while
   after the last kill; with `on_combat: stop` each leg failed instantly. Suggest: ignore combat when no hostile
   is alive and aware.
8. **Activation reach is distance only** (medium). `activate` checks distance, not line of sight or facing, and
   needs the agent to stand within 300 units; to try pillars and lever from one spot the run used
   `max_distance` up to 560, which a real player could not do. Suggest: line-of-sight check and reporting
   which side of a wall the target is on.
9. **Quest stage lags the dialogue** (low). Stage 10 appeared about 10 s after the last topic; polling
   `quest_get` too early reads stage 5 and an empty objective list. Suggest: `dialogue_close` waits for quest
   changes it caused (event `questStage`).
10. **Combat needs line of sight** (medium). `Combat.state` lists hostiles behind gates and walls as targets, so
    the fight loop walks into them. Suggest: `hasLineOfSight` per hostile, and `combat_fight` skips the ones without.
11. **`look` is relative** (low). After `Combat.face` the pitch stays tilted (the agent looked at the floor several
    times); a `level.sh` helper read the pitch and undid it. Suggest: `look {absolute: true}`.
12. **Host start timeout of 12 s orphans the game** (medium). With a slower host the start reply timed out
    while the game launched anyway, leaving a running game with no session. Raised to 45 s here.
13. **Footage handling** (process, about 25 min). A 25 minute capture is 1.5 GB and copies at about 270 KB/s;
    transcoding on the PC (1280 wide, crf 32, 80 s) first made it 200 MB. Suggest `capture_stop` does that.
14. **PC contention** (process, about 60 min total). Lock waits of 8, 29, 10 and 10 minutes, and a parallel agent starting
    games while the lock was held (game-start failures and a stopped session for about 5 minutes).

## Evidence kept locally (not committed)

Screenshots live under `.local/playtests/golden-claw/`, the tool-call log in `toollog.jsonl`, the composed video
in `.local/videos/golden-claw.mp4`. Nothing game-derived is in this repository.

## Addendum: a full run with the perception tools

A second run took the quest from the pillar room to Lucan's reward (MS13 stage 100, completed) with the new
perception tools (`world_inspect`, `world_models`, `screen_capture` with zoom and crop, dead-end `blockers`)
and the idle vanity camera switched off. About 110 minutes of play; a 4:45 highlight cut was composed from
the narration markers with `--highlights`.

**How the puzzles were read.**
* Pillars: `screen_capture` zoomed on each stone face over the gate (the middle tablet lies on the floor and
  was read there); `world_inspect` gave each pillar's node rotation, so one push established the cycle order
  instead of guessing from pixels. The first guess read the tablets mirrored and kept the gate shut (one
  lever pull wasted); the direct reading opened it.
* Claw door: `world_models contains=claw` found the door rings; the claw's palm was read by rotating it in the
  inventory and cropping with UI kept (`include_ui` was added for this). The rings were set from the reading.
* Webs, chains, the exit: `world_inspect`'s "activates" field named the portcullis a chain opens and the handle
  that opens the secret exit; `world_models contains=web` found the web door the navmesh stops at.

**Fallbacks and findings (logged, not hidden).**
* God-mode fallback: after three deaths to draugr groups (level 1 character, no potions), the rest of the
  barrow was walked with `nav_goto invulnerable=true on_combat=fight`. A player would have levelled first.
* Objective missed: navigation walked past the word wall, so Lucan had no topic for the claw. A save before
  the sanctum was reloaded and the wall found with `world_models contains=word`. No tool shows where the
  current quest target is.
* Nav ping-pong (high): from the barrow exit, `nav_goto place=Riverwood` re-planned through the exit door it had
  just used and walked in and out for about 12 minutes. A `route=global` goal set from outside worked; no fast
  travel was used. Fix: after a door transition, do not plan back through the same door; prefer the global
  route right after a door.
* Low frame rate (high): `Perf.frameStats` read 10 fps with the desktop capture running (median frame 16.7 ms,
  p99 1.4 s, hitches up to 14-31 s). 10 s after stopping the capture: 4.8 fps; walking: 3.7 fps; 20 s settled:
  14.9 fps, p99 0.7 s. Stopping the capture did not remove the hitches, so the capture is not the main cause;
  multi-second stalls during long sessions are. Not yet diagnosed.

**Improvements, most costly first** (time is this run's estimated loss).
1. Multi-second hitches / low fps (~40 min across nav, fights and dialogue): log each hitch with cell loads,
   streaming and host CPU in `Perf.frameStats`; take an A/B at session start as routine.
2. No quest-target tool (~25 min re-run after missing the word wall): `quest_targets` returning the objective's
   alias refs and positions, as the compass marker shows a player.
3. Combat at level 1 without potions (~20 min, three deaths): `combat_fight` retreats and heals below a
   threshold, and reports "outmatched" early so the agent can choose a fallback.
4. Nav door ping-pong (~15 min): do not re-plan through the door just used; global route after any door.
5. Nav planner returns to an old dead end after a gate or web opened (~10 min): invalidate cached dead ends
   when a nearby `World.inspect` open state changes.
6. Plugin build rounds from header differences between the Mac and PC CommonLib (~10 min): build against the
   PC headers only, or sync them into the worktree.
7. `nav_wait` times out (RPC -32008) during cell loads (~5 min): treat a load as progress, not a timeout.
8. Blocker heuristic blamed the wrong web (~5 min): rank candidates by whether they lie on the straight line
   to the goal and by load-door links; include a crop of the stop view.
9. Inventory navigation ignored `press_key`; `press_control Accept/Down` worked (~5 min): document it in the
   menu tools, or add `ui_inspect_item`.
10. `combat_unequip` left the sword off after a conversation (~3 min): re-equip the previous weapon on draw.
11. `screen_capture` dropped menus (~3 min): fixed here with `include_ui`.
12. Stale daemon after a rebuild (~3 min): the server warns when its own code is older than the files.
13. Arrival radius 150 versus activation reach (~3 min): `nav_goto` with `ref_id` stops within activation reach.
14. Lock refresh during long holds (minor): refresh automatically while a game session is open.
15. Long footage and a full Mac disk (~10 min): `--emit-dir` composes on the host; a `capture_stop transcode`
    step would make the copy small enough to fetch.

## Addendum: low frame rate investigated

* Not our plugin. `Perf.hitches` and per-call timing were added; in 5-6 minute visible runs with the desktop
  capture, status polling, nearby scans and a Riverwood/trader/barrow navigation loop the game held 59.7-60 fps,
  p99 16.9-17.2 ms. The one plugin cost found was the first `Nav.goto` reading its worldspace graph on the
  game thread (78 ms, once 458 ms); the graphs are now read in the background (max 7.7 ms).
* Not the disk. A 12 GB sequential write on the system SSD during play (1.7 GB/s peak, queue 2.8, 2.9 ms worst
  transfer) produced no hitch.
* The old session's evidence: the capture log shows the whole desktop's frame delivery falling from 30 fps in the
  first 20 minutes to 5-6 fps from minute 50, steadily, while the tool-call rate fell. The plugin log shows
  the game thread blocked in a wait (not computing) for 6 s or more from minute 50. The degradation is
  system-wide and time-dependent, not tied to our calls. Not reproduced inside the 20-minute lock limit.
* Also found: the visible profile used exclusive fullscreen, which drew the game in a corner of the monitor and
  made desktop capture black; visible runs are now borderless-windowed (`Window.info` confirms 2560x1440
  windowed on a 2560x1440 monitor). Resolution made no difference to the hitches in the runs above.
