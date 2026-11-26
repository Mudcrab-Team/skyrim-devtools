# Playtest: A Lovely Letter (Riverwood love triangle)

Quest `FreeformRiverwood01`. Played through the skyrim-use tool set only (the MCP tool functions, driven by a
small local daemon that forwards one call per line; no other path into the game), windowed fullscreen at the
game's native resolution, offline test profile, English, normal game speed.

Result: **passed**. Quest stage reached 310 (all six objectives resolved), Faendal paid 25 gold (140 -> 165).
Live time from `game_start` to the end of the final conversation: about 3.5 minutes (207 s of footage), 138
tool calls. Waiting for the shared PC lock before the session cost about 25 minutes.

## The decision

First pass, before acting: Faendal and Sven both court Camilla. Both are schemers, but the thoughtful reading
at the start was "Sven is loud and vain, Faendal is the quiet underdog", and the first caption said "I side
with Faendal". That was a guess made before reading the quests.

Second pass, after Faendal's first conversation (stage 10): his request is to hand Camilla a forged letter
"from Sven" to make Sven look foolish. That is a lie to a third person who has no say, so the underdog reading
was wrong. Revised plan: side with Sven, who was the one being framed, tell him what Faendal did, and take his
counter-letter to Camilla.

Third pass, in the shop (stage 105): Sven's counter-letter is also a forgery, this time in Faendal's voice, and
Camilla's dialogue offers two choices: "I have a letter from Faendal. (Lie)" or the truth ("Sven wanted to
trick you ..."). Honesty outranked loyalty to a side that had just proven to be playing the same game. I told
the truth. The cost: it exposes Sven. The consequence: Camilla thanked me, sent me back to Faendal (stage 240),
and the story ends in Faendal's favour (stage 310) with 25 gold. So the agent's final answer to "Who would it choose?" is: nobody's scheme. It chose the truth,
and the truth happened to favour Faendal. The other branch (lie, Sven's side) is scripted in
`tools/scenarios/a_lovely_letter_sven.py`; it ran live on the first attempt in 66 s and ended at stage 230
(Camilla believes the forged letter; the player still has to report to Sven afterwards).

## Condensed tool-call log

| # | Calls | Result |
| --- | --- | --- |
| 1 | `game_start {headless:false, save:"Fixture"}`, `wait_for playable` | Fixture already stands in Riverwood next to Faendal; no setup cheat needed |
| 2 | `world_nearby types=[actor]` | Faendal 135 u away, Sven 1230 u, refIDs known |
| 3 | `quest_get FreeformRiverwood01` | found on the first guess of the editor ID; stage 0, not active |
| 4 | `dialogue_start 0001348C`, `dialogue_wait skip`, `dialogue_select 1` "problem with Sven", `dialogue_wait`, `dialogue_select 0` "You hope?" | stage 10, objective "Deliver Faendal's letter to Camilla" |
| 5 | `look` + `press_key W hold` x3 (own helper loop, see friction 2) | reached Sven, 94 u |
| 6 | `dialogue_start Sven`, select "Faendal wanted me to deliver this letter" | stage 105, Sven's counter-letter |
| 7 | `world_nearby types=[door]`, `rpc_call Ref.get` on doors | owner faction identifies the Riverwood Trader door |
| 8 | walk, `world_crosshair` until the door shows, `press_control Activate` | interior `RiverwoodRiverwoodTrader` |
| 9 | walk to Camilla, `dialogue_start`, `dialogue_select 0` (truth) | stage 240, "Talk to Faendal" |
| 10 | leave via door 000133E9, `world_nearby` to find Faendal outside, walk, `dialogue_start`, select "Camilla will not be spending any more time with Sven" | stage 310, +25 gold |
| 11 | `capture_stop`, `game_stop`, compose | cleanExit, 207 s footage, 176 s video |

Stages observed: 0 -> 10 (Faendal's letter) -> 105 (told Sven) -> 240 (told Camilla the truth) -> 310 (thanked by
Faendal). Objective 20 and 110 completed, 10 skipped to completed, 120 and 220 completed silently.

## First attempt that failed

The first launch used a plugin build deployed by another agent (a nav branch based before the dialogue flow).
`Dialogue.waitIdle`, `Dialogue.advance` and `phase` were missing, `Dialogue.select` answered `called:true` but
the conversation never advanced, and the topics, subtitles and quest stage stayed unchanged for 18 s. I stopped,
built current main with `tools/pc-build --name quest --deploy --keep-lock` (25 s end to end) and the second run
went through. About 6 minutes lost.

## Friction, bugs and missing tools

1. **Stale plugin on the PC, silent failure** (high). A DLL older than the Python wrappers made
   `dialogue_select` return success while nothing happened, and `dialogue_state` had no `phase` to tell the
   difference. Suggest: `game_start` returns the plugin build id and the list of missing curated methods; a
   wrapper whose method is absent already errors, but `Dialogue.select` existed in both builds, so also expose
   `protocol` minor version per method group.
2. **No walking tool on main** (high). I wrote a 25-line loop (aim with `look`, hold `W`, re-read `player_state`).
   Distances were short and it worked, but it needs the yaw convention (bearing clockwise from +Y, `look`
   takes a signed delta) that nothing documents. Suggest: `nav_goto {ref_id | x,y | name}` and `nav_status`
   (the nav branch), and meanwhile document the yaw convention in protocol.md.
3. **Doors are anonymous** (medium). `world_nearby` lists seven "Wooden Door" entries; the only hint to which
   one is the shop is `owner` from `Ref.get`. Suggest: `teleportDestination` (cell name and editor ID) on door
   references, and a `types:["door"]` filter that includes it.
4. **Activating needs crosshair micro-aiming** (medium). The door reference origin is at the hinge: walking to
   the reported position put the crosshair on the wall. It took 4 extra calls. Suggest: `activate {ref_id}`
   that walks into range, aims at the reference bounds centre and activates, or `Ref.activate` natively.
5. **`quest_get` flags look wrong at the end** (medium). At stage 310 the quest reports `completed:false`,
   `enabled:false`, `active:true`. Suggest: document what each means, and add `finished` derived from the
   quest's own completion flag, so a scenario can assert one field. The stage number is the reliable signal today.
6. **Editor ID of a quest is a guess** (low). `help "lovely letter" 0 QUST` finds nothing because quest names are
   not searchable by the console. I got it right by trying `FreeformRiverwood01`. Suggest: `quest_find {name}`
   (substring on display name) returning editor IDs.
7. **`dialogue_wait` shows only the new lines, not choices that carry a tag** (low). The "(Lie)" tag is part of
   the text, which was good, but nothing marks a persuasion or honesty choice structurally. Suggest a `tags`
   array (`lie`, `persuade`, `intimidate`, `bribe`) per topic when the game marks it.
8. **`dialogue_close` returns the whole state** (low, noise). 700+ characters per call in a long log. Suggest a
   compact `{status:"closed"}` reply.
9. **Quest tracker block in the video stays empty until the first status sample** (low). Cosmetic.
10. **PC lock fairness** (process, about 25 min). Two other agents cycle sessions; the waiting agent polls every
    45 s and lost the race several times. Suggest a FIFO ticket file next to `agent.lock`.
11. **Pitch stays at about 10 degrees after loading**, so the default crosshair misses short targets (low).
    `look {pitch_deg}` is relative, which makes levelling a two-step. Suggest `look {absolute:true}`.

## Evidence kept locally (not committed)

Screenshots and the raw tool-call log live under the maintainer's local playtests folder; the composed video is
in the local videos folder. Nothing game-derived is in this repository.
