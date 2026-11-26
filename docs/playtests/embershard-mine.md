# Playtest: Embershard Mine (bandits near Riverwood) and a long-session soak

Goal: play an easy quest from the Fixture save, visible 2560x1440 borderless, for 60-75 minutes with
`windows/perf-watch.ps1` and `Perf.hitches` running, and catch the late frame-rate collapse. Played through the
skyrim-use tool set (a local daemon forwarding one tool call per line), capture on, offline test profile.

Why this quest: Embershard Mine is a short bandit dungeon 1.5 km from Riverwood with no puzzle, and the task allowed
it. It has no journal quest (`dunEmbershardQST` stays at stage 0 with no objectives), so `quest_targets` returns
nothing; "cleared" was defined as no living hostile actor left in the cell.

Result: **not finished**. 3 of 6 bandits were killed (two on the north walkway, one in the south camp); the three
in the west were not reached (one archer stands on a bridge, 400 u above the floor the path leads to). The game
ended four times (clean exit, hang, crash, kill), so the quest needs another slot.

## Kit (openly given, not god mode)

Fixture character is level 1 with iron armor, an iron war axe, five minor healing potions. Added with the console
before the quest, each session: 10 `Potion of Healing` (`additem 0003EADE 10`), a Steel Sword (`additem 00013989`),
iron shield equipped on the left hand. `combat_fight` said "outmatched" at once against two level-2 wolves
(22 HP each) and was overridden with `stand_ground=true`; nothing used `invulnerable`.

## Sessions

| # | Start (UTC) | Length | Ended by | Notes |
| --- | --- | --- | --- | --- |
| 1 | 08:23 | 14 min | game exited by itself, exit code 0, no crash log, "unrequested-exit" | wolves, road, mine entrance, first dead end; 60 fps to the end |
| 2 | 08:39 | 36 min | main thread hang, "Not Responding" at 09:08:32, killed at 09:19 | lever, bridge, first bandits; hitches grew from minute 15 |
| 3 | 09:21 | 15 min | crash in `XAudio2_7.dll` (access violation, audio worker thread) | AI pathfinder, two bandits, archer stalemate |
| 4 | 09:38 | 4 min | stopped by me | same build with `bEnableAudio=0` (see below) |

## Frame rate over time

`perf_check` (10 s windows), Perf.frameStats:

| Time (UTC) | Run / game minute | fps | p99 ms | max ms | hitches |
| --- | --- | --- | --- | --- | --- |
| 08:23 | 1 / 0 | 60.1 | 16.8 | 17.2 | 0 |
| 08:26 | 1 / 3 | 60.1 | 16.8 | 16.9 | 0 |
| 08:39 | 2 / 0 | 60.0 | 16.8 | 17.1 | 0 |
| 08:54 | 2 / 15 | 54.3 | 53.1 | 321 | 4 |
| 09:07 | 2 / 29 | 45.2 | 97.4 | 934 | 5 (blocked 0.9-1.7 s each, thread CPU 0) |
| 09:08:32 | 2 / 30 | main thread stops answering; first plugin warning 09:08:39 "main thread has not run a queued task for 7.3 s", then 27 s, 48 s ... 108 s | | | |
| 09:13 and 09:15 | 2 / 35 | 0 frames in 10 s | | | |
| 09:21 | 3 / 0 | 60.0 | 16.8 | 17.1 | 0 |
| 09:42 | 4 / 4 | 24.9 | 952 | 1105 | 8 |

The four-minute collapse in run 4 happened with another agent compiling on the same PC (`cl.exe`, `ninja` visible in
the perf-watch top list at other times), so that row is not a clean reading.

Host counters (`perf-watch`, 5-minute means) during run 2, UTC:

| Window | total CPU % | SkyrimSE CPU (perf-watch units) | avail MB | GPU 3D % |
| --- | --- | --- | --- | --- |
| 08:37-08:42 | 16 | 23 | 20489 | 13 |
| 08:42-08:47 | 21 | 26 | 20269 | 18 |
| 08:47-08:52 | 22 | 44 | 20715 | 20 |
| 08:52-08:57 | 34 | 72 | 20840 | 15 |
| 08:57-09:02 | 42 | 121 | 20905 | 16 |
| 09:02-09:07 | 56 | 225 | 20923 | 11 |

Memory, paging, disk queue and GPU stayed flat; the game's own CPU rose tenfold. At the hang the game process used
about 6 cores (CPU time 7790 s -> 8675 s in 135 s) while its main thread was blocked and GPU load was 0.

## What the hitches were

`Perf.hitches` rows during the collapse: every hitch had `blockedMs` equal to `ms` and `threadCpuMs` 0, no
overlapping plugin call, no disk read, few page faults. The frame thread was waiting, not computing, and nothing of
ours was running on it (`slowCalls` max 8 ms, once, for the first `Nav.goto`). The plugin's stall watchdog logged the
main thread's stack during the hang: a wait inside the game executable, entered from the engine's own thread start.

## Thread count

`Get-Process SkyrimSE` thread count, sampled once a minute:

| Run | Minute | Threads |
| --- | --- | --- |
| 3 | 0 | 135 |
| 3 | 2 | 383 |
| 3 | 5 | 688 |
| 3 | 8 | 1100 |
| 3 | 11 | 1583 |
| 3 | 14 | 2109 |
| 2 | 30 | 3507 (3579 at 35 min while hung) |
| 4 (audio off) | 0 / 1.5 / 4 / 5 | 105 / 213 / 282 / 333 |

The count grows without bound, 2 per second during play and about 13 per minute while the game is hung. Run 3 ended
in a crash inside `XAudio2_7.dll`; run 2 hung; the very first launch of the day also crashed in the audio manager
thread. All of the PC's audio endpoints report state Unknown (no output device attached). Switching the engine
audio off (`bEnableAudio=0`, now the `disableAudio` host config key) cut the growth to about 40-50 per minute but did
not remove it, so audio is a large contributor and not the only one. The growth tracks activity (fights, navigation)
more than wall-clock time. The working set of the hung process had been trimmed to 135 MB of 5.3 GB private, which
is a symptom of the stall, not a cause (20 GB RAM free).

## The Not Responding episode

* 09:07:48 perf_check: 45 fps, five hitches up to 1.7 s. 09:08:32 main thread stops answering.
* The desktop capture shows a frozen last frame from about 09:09 (the frame at 30 min and the final frame are
  byte-identical); `Window` state: the process had no main window handle, the OS had ghosted it.
* Session events: the only Kernel-Power session transitions in the previous 80 minutes were a SessionUnlock at 09:08:17 and an
  input wake at 09:08:31, both within seconds of the first stall. No lock screen, screen saver or LogonUI process was
  running, no display-driver (TDR) event was logged, Defender and GlassWire were not in the top CPU list (MsMpEng
  appeared in 9% of samples, at 1-4% of one core). The interactive session showed 13 minutes idle: injected input
  does not count as user activity, so the display idle timeout (15 min) is a standing risk for a visible session.
* Restoring focus (`capture_start`, which re-focuses the game) did not recover it within 3 minutes. `game_stop` then
  reported "Windows shutdown timed out" and left the interactive task in state Running plus a stale session marker, so
  `game_start` failed three times ("Interactive task is busy", then "file already exists") until both were cleared by
  hand (about 7 minutes).

Conclusion: no single external cause was found. The evidence points at the game process itself (unbounded thread
growth in a no-audio-device environment ending in a wait deadlock or an XAudio2 crash), not at disk, memory, GPU,
Defender, the capture or the plugin. Suggested next experiment: a visible run with a virtual audio output device
installed (so XAudio2 has an endpoint) and one with `disableAudio`, both for 45 minutes with the capture off, thread
count logged every minute.

## Navigation and combat findings

* **Dead-end loops (high, about 25 min in total).** `mode=input` stopped at a low ledge ("closed activator: Lever")
  and `combat_fight` then idled for 100 rounds twice. The lever it blamed was unreachable; the real one was the second
  lever on the north walkway, pulled with `nav_goto activate=true`. After it the bridge was open but absent from the
  navmesh. `mode=ai` reached the south camp in 60 s where `mode=input` had failed for 20 min; three west bandits
  still failed in `input` mode with blockers naming torches ("closed activator RemovableTorch01", "Torch Sconce"),
  which are not blockers: `likely` was wrong again.
* **Stuck loops in my own scripts.** The loop re-planned toward the same unreachable bandit three times because a fix
  to the script silently did not apply; the maintainer's rule (distance not improving in 15 s means stop) was added
  afterwards, with a skip list.
* **Combat stalemate (about 8 min).** An archer on a bridge 417 u above the player: melee cannot reach him and
  `combat_fight` loops until `timeout`. Needs a ranged option (`combat_shoot`) and a "reachable?" check.
* **No quest marker in a dungeon without a quest.** Listing actors, containers and doors with `world_nearby` (radius
  12000) gave a usable map in one call; it should be a tool (`explore`).
* **`combat_fight` outmatched** triggered on level alone (two wolves of 22 HP); `stand_ground=true` was right.
* **Potions** ran out after 4 drinks in one long fight; `combat_fight` reports the failure only at the end.

## Bottlenecks (estimated time lost)

* Dead-end/stuck loops in the mine: about 25 min.
* Recovery from the hung game (stop, stale task, stale marker): about 7 min.
* Archer stalemate: about 8 min.
* perf-watch samples every 3-4 s, not 1 s (Get-Counter GPU query), and stopped writing when its parent job ended at 09:08.
* `pc-build hold --max-minutes` ended the daemon but the lock file was left behind and went stale.
* Uploading the composer inputs to the PC: 130 MB took over 5 minutes on the link; footage cannot be fetched to the
  Mac (3 GB).

## Video

The composer gained two small features in this change: `holdS` on a `narrate` timeline record keeps a caption up for
that many seconds without stopping the speed-up (used to label the frozen stretch), and calls that failed after
more than 3 s count as waits. The full timeline is kept and sped up (15x idle, 20x walking, 1.5x fights), no cutting:
three parts joined. The long freeze is shown with the label "Game froze (Not Responding)".
