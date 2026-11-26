# DevTools protocol 2

One JSON-RPC 2.0 message per WebSocket text frame at `/rpc`. The listener binds to Windows
loopback, requires the per-run bearer token, rejects browser origins and accepts one
controller at a time. Frames may be up to 16 MiB in either direction.

Several calls may be in flight at once (up to 64). Each method has its own deadline. When a
deadline passes, the call fails with `-32008` and the session stays usable. If the handler
finishes later, its result is dropped. Server notifications have no `id`:

~~~json
{"jsonrpc":"2.0","method":"event","params":{"type":"menuOpenClose","seq":12,"epoch":1,"data":{"menu":"InventoryMenu","opening":true}}}
~~~

`Session.getInfo` returns the protocol number, build, image hash, session id, `dataLoaded`,
`loadEpoch`, `headless`, `pluginBuild` (git sha, `-dirty` when native/ had local changes, and a hash of native/; `unknown` for a hand build; absent in DLLs that predate it), `capabilities` (`console`, `events`, `headless`, `input`, `loadSave`,
`menus`, `perf`, `screen`), every registered method with its thread and timeout, and bus counters.

## Methods

| Method | Thread | Params | Result and meaning |
| --- | --- | --- | --- |
| Session.getInfo | any | | Identity, capabilities, method list, bus stats |
| Session.ping | any | payload? | `{pong, payload}`. Keeps an idle link alive. |
| Session.pingMain / pingUI | main / ui | | Round trip through the game's task queues |
| Session.close | any | | Replies `{closing}`, then closes the game window |
| Session.listSaves | any | | `[{name, bytes}]` in the isolated Saves directory |
| Session.loadSave | main | name, wait? | Loads any save in Saves, in game or from the main menu. Increments `loadEpoch`. With `wait`, replies after the load finishes. |
| Session.reload | main | wait? | Loads the last loaded save again |
| Events.snapshot | any (deferred) | | `{seq, loadEpoch, dataLoaded, loadCompleted, loadSucceeded, playable, menus:[{name, ready}], foreground, headless, lastSave, uiBusy}`. The UI thread answers; if it runs no tasks for 1.5 s (a long load, a paused window) the connection thread replies with the last known `menus` and `playable` and `uiBusy: true`. |
| Events.list | any | | Declared event types with descriptions |
| Events.subscribe / unsubscribe | any | types (`"*"` for all) | Selects pushed event types. Resets on every connection. |
| UI.openMenu / closeMenu | ui | name, wait?, anyState?, loadEpoch?, force? | Any registered menu. `wait` replies once the menu actually opens or closes. Unless `anyState` is set, the world must be playable.  Trainer, barter, gift, container, lockpicking, crafting, dialogue and book menus need the engine's own context: `openMenu` refuses them unless `force`, and `closeMenu` leaves them with the Cancel control (hiding one directly froze the game).|
| UI.listMenus | ui | | `[{name, open}]` for every registered menu |
| UI.isMenuOpen | ui | name | `{menu, open, ready}` |
| UI.getVariable / dump | ui | menu, path, depth? | Bounded read of a Scaleform value of an open menu, from `_root` (`Menu_mc.ItemCard_mc`, numeric steps index arrays). Objects deeper than `depth` come back as `"[Object]"` and `truncated` marks the node cap. |
| UI.setVariable / invoke | ui | menu, path, value / args | Writes a variable, or calls an ActionScript function and returns its result |
| UI.callDelegate | ui | menu, name, args? | Calls a native function the menu registered on its GameDelegate |
| UI.showMessageBox | ui | text, buttons? (1-4) | Raises a message box. `UI.messageBox` then reports `lastAnswer`. |
| UI.messageBox | ui | | `{open, text, buttons:[{index, text}], cancelIndex, cancellable}`, or `{open:false, lastAnswer}` |
| UI.messageBoxSelect | ui | index or text | Presses a button (text matches exactly ignoring case, else a unique substring) |
| UI.listItems | ui | menu | `{count, selectedIndex, items:[{index, name, count, equipped, equipState, category, favorited, formId, value, weight, owner?}]}` for InventoryMenu, ContainerMenu, BarterMenu, FavoritesMenu. `UI.selectItem` replies after the menu rebuilt its list, so a following `UI.listItems` is current. |
| UI.selectItem | ui (deferred) | menu, index or text, action?, count? | `equip`/`use`/`unequip`/`drop` (Inventory, default `equip`), `take`/`transfer` (Container, Barter), through the game's equip and inventory APIs. After a change it sends the menu's own inventory-update message and waits (up to 1.5 s) for the list to change; the reply adds `after` (the entry's new state, null when gone) and `listUpdated`. |
| UI.list / UI.select | ui | menu, index or text, path?, press? | Entries of other list menus and picking one |
| UI.notifications / subtitles | any / ui | since? | Recent HUD notifications `{seq, text}`; subtitles on screen now |
| Dialogue.state | ui | debug? | `{open, phase, selectable, speaker:{refId, name}, greeting, waitingToAdvance, canSkip, topics:[{index, text, neverSaid}], lastLine, cursor, subtitles, submenu?}`. `phase` is `closed`, `npc-speaking` (an NPC talks but the menu is not up), `speaking` (a line plays, topics still show the previous list or a lone "..."), `advancing` (between lines), `topics` (selectable) or `submenu`. Also valid for conversations the NPC started. |
| Dialogue.select | ui | index or text, force? | Picks a topic through the dialogue menu's own `TopicClicked` handler. Fails with busy (-32009) while a line is spoken unless `force`. Returns `cursor`. |
| Dialogue.waitIdle | any (deferred) | timeoutMs? (30000, max 120000), skip?, since?, openGraceMs? | Returns when the topics are selectable again (`status: topics`, or `idle` without topics), the menu closed (`closed`), another menu opened over the dialogue (`submenu`, with its name in `submenu`) or the time ran out (`timeout`). Also `lines:[{seq, speaker, role: player\|npc, text}]` spoken since the last select/start/wait (or `since`), `waitedMs`, `cursor` and the fields of `Dialogue.state`. `skip` skips every line once it was captured. |
| Dialogue.advance | ui | | Skips the spoken line (the menu's `SkipText` handler, what a click during speech does). `{skipped}` |
| Dialogue.close | any (deferred) | timeoutMs? | Leaves the conversation through the menu's own exit handler, closing a menu opened from it first. `status: closed`; idempotent. |
| Dialogue.start | main (deferred) | refID?, loadEpoch?, wait? (true) | Activates an NPC (default the nearest living actor); with `wait` replies once the dialogue menu is open (`opened`). |
| Console.execute | main | command, target?, loadEpoch? | Runs any console command. `target` is `selected` (default), `player` or `none`. Returns `{command, lines}`, every line printed while it ran. |
| Console.history | any | since? | Recent console lines from any source, `{lastSeq, lines:[{line, lineSeq}]}` |
| Input.key | any | key, phase?, holdMs? | DirectInput scancode (1-255) or name (`Tab`, `E`, `Grave`, `F1` ...). `phase` is `tap` (default), `down` or `up`. |
| Input.control | any | name, phase?, holdMs? | ControlMap user event (`Journal`, `Tween Menu`, `Activate` ...), resolved to its bound key in the innermost active input context |
| Input.mouse | any | dx, dy or button, phase?, holdMs? | Relative motion, or button `left`, `right`, `middle` or 0-7 |
| Input.releaseAll / Input.stats | any | | Releases held keys / `{hooked, polls, msSinceLastPoll, injectedButtons, injectedMoves}` |
| Player.state | main | avs? | Position, rotation (deg), cell, worldspace, health/magicka/stamina, level, gold, carry weight, stance flags, active effects. `inCombat` is true only while a living, loaded actor has the player as its combat target; `combatFlag` is the engine's raw flag (it can outlive a fight after a teleport or load) and `combatants` the count. `avs` adds named actor values. |
| Player.inventory | main | filter?, limit? | `{total, items:[{formID, name, type, count, value, weight, equipped}]}`. Filter: weapon, armor, ammo, potion, ingredient, book, misc, key, scroll, soulgem, note, or a 4-letter record type. |
| Player.equipped / Player.spells | main | kind?, limit? (spells) | Hands, worn armor with slots; known spells, powers and shouts |
| World.nearby | main | radius?, types?, limit? | References sorted by distance: actor, container, door, item, activator, furniture, flora. Actors carry level, healthPct, hostile, dead, inCombat, race. Doors carry `locked`, `lockLevel` and, for teleport doors, `destination` {cell, cellEditorID, interior, location, worldspace, doorRefID, pos}. |
| World.crosshair | main | | `{ref}` under the crosshair with the activation `prompt`, or null |
| Form.get / Ref.get | main | formID or editorID / refID | Basic form or reference info. Editor IDs resolve only for record types the engine keeps them for. |
| Quest.get / Quest.active | main | editorID or formID, allObjectives? / limit? | Stage, flags, objectives with state; quests with a displayed objective. `status` is running, completed, failed, stopped or not-started and `finished` is true for completed or failed. The raw `completed` flag is set only by a script's CompleteQuest and `running` (the library's IsRunning) stays true after a quest is stopped; a quest normally ends by a shut-down stage, which clears `enabled` without setting `completed`. So `status` is derived: failed (failed flag), completed (completed flag, or stopped after running with every objective completed), running (enabled), stopped (ran, stopped, not finished) or not-started; `finished` is true for completed or failed and `finishedBy` names the evidence (`completedFlag`, `objectivesCompleted`, `failedFlag`). `status` and `finished` are the fields to assert on; `finishedBy` names the evidence (`completedFlag`, `shutDownStage`, `failedFlag`). |
| Quest.targets | main | quest? (editor id, form id or name; default every running quest), limit? (20), includeCompleted? | What the compass and map quest markers point at: for each displayed objective of the quest(s), `{quest, questName, formID, tracked, objective, text, alias, target, tracking, distance}`. `target` is the reference the marker is on and `tracking` the reference the compass follows now (the way to the target, usually a load door, when it lies in another cell); each is `{refID, name, pos, cell, interior, baseType, sameSpace, loaded, distance}` with `distance` null when in another cell. The tracked quest comes first. An empty list means the quest shows no marker now. |
| Quest.find | main | text, limit? | Quests whose display name or editor ID contains text |
| Ref.activate | main | refID, maxDistance?, force? | Activates a reference like the activate key without aiming. Refuses beyond maxDistance (default 300, to the nearer of origin and bounds centre) with `activated:false`; walls are not checked (`lineOfSightChecked:false`); force skips the reach check |
| Global.get / Game.time | main | editorID or formID / | Global value; hour, day, month, year, daysPassed, timescale |
| Game.setTimescale | main | value | Sets the TimeScale global, returns the time |
| Game.setSpeed | main | walk? (SpeedMult percent, 100 normal, 25-400; `force` lifts the cap to 1000; applied as a temporary modifier followed by a net-zero carry-weight nudge, which is what makes the movement code recompute its cached speed; the request is a percent, not a guarantee: the delivered speed is reported as `measured` once a Move.walk has run, so compare that, not the request), timescale? (0-10000), reset?, force? | Sets the player walk speed multiplier and/or the clock scale and returns `{walkPercent, original, manual, autoBoost, speedMult, speedMultBase, timescale, measured?}`; `measured {unitsPerSecond, atPercent, perPercent}` is the steady speed the last Move.walk delivered and the percent it was walked at. `reset` drops the manual setting and any automatic boost and restores the original value. A save load forgets the bookkeeping. Event `speedChange`. |
| Game.speed | main | | The same reading without changing anything |
| Game.waitHours | main | hours (0-720) | Skips game time by moving the calendar globals forward; also moves the engine's own day counters (`rawDaysPassed`, `midnightsPassed`) and the day/month/year globals, because the engine rebuilds `daysPassed` from them and would otherwise undo the skip; returns `{waitedHours, hourBefore, hour, daysPassedBefore, daysPassed, rawDaysPassed, rawIsTotal, midnightsPassed, day, month}`. Event `timeSkipped`. Scripts and schedules catch up on their next update; the hours are not simulated. |
| Game.mapMarkers | main | contains?, discoveredOnly?, limit? (50) | Placed map markers by displayed name: `{count, markers:[{name, marker, visible, canTravelTo, pos, distance}]}`. Scans every reference, so it takes a moment. |
| Game.fastTravel | main | to (map marker name, partial names accepted, or a marker refID; a location name is the fallback), force?, skipTime? (true) | Moves the player to the named map marker (MoveTo, with the usual loading screen) and advances the clock by about distance / 6500 hours. Without `force` the marker must be visible and travelable (discovered) or the call fails with `-32010` and `{visible, canTravelTo}`. The map UI is not driven. Reply `{to, marker, markerPos, distance, forced, from, time?, hours?}`; event `fastTravel`. |
| Scenario.character | main | level?, skills? ({av: value}), perks?, spells?, items? ([name, formID or {item, count, equip}]), clearBounty?, weather? (form id or editor id) | Builds the character in one call and reports every part as `{step, ok, reason?, ...}` in `steps` (a bad name fails its own step only). Items, perks and spells resolve by displayed name (exact, case-insensitive) or form id; skills set the base actor value; level goes through the console `player.setlevel`; clearBounty zeroes every crime-tracking faction; weather is forced. Event `scenarioStep` per step. |
| Scenario.setStage | main | quest (editor id or form id), stage | `startquest` then `setstage` through the console, so stage fragments run as for a player; returns `{quest, stage, before, after, wasRunning, console}`. The engine does not expose a quest's stage list, so earlier stages have to be named by the caller. |
| Player.teleport | main | x,y,z or refID, yawDeg? | Position set, or MoveTo a reference (queued by the engine, so the reply carries the target) |
| Player.setAV / addItem / removeItem | main | av,value / formID,count | Return before and after values |
| Player.look | main | yawDeg?, pitchDeg? | Turns the player by the given amount, relative to the current view. Pitch is clamped to ±85°. |
| World.centerOnCell | main | cell, loadEpoch? | Stops combat on the player and on everyone fighting them (`combatCleared` = actors stopped), then `coc <cell>`; returns the console lines |
| Screen.capture | any (deferred) | maxWidth? (1024), format? (`png` or `jpeg`), quality? (85, jpeg), includeUI? (true), path?, crop? ({x, y, w, h}, fractions 0-1) | Reads the game's own swap-chain back buffer on the render thread just before Present, so it works headless and off-screen and never uses desktop capture. Scales down (never up) and encodes with WIC. Returns `{frame, width, height, sourceWidth, sourceHeight, format, bytes, data, readbackMs, encodeMs, totalMs}` with base64 `data`. With `path` (a plain file name) the image is written to the run's `screens/` directory and `path` replaces `data`. `includeUI: false` hides menus and the HUD for a few frames and restores them. `crop` cuts that part of the full-resolution frame before scaling, so a crop of a distant carving keeps its detail. |
| Screen.project | main | refID or x, y, z | Where a reference's world bound (or a point) lands on screen: `{center:[x,y], rect:{x,y,w,h}, onScreen, inFront, worldCenter, radius, ref?, note?}` in fractions of the frame, for cropping a capture |
| World.inspect | main | refID, nodes? (true), graph? (true), scripts? (false) | One reference in depth: base form (type, name, editorID), model, position and angles, open state (`open`, `opening`, `closed`, `closing` or null), locked, disabled, linked refs, enable parent, activation parents and children, world bound, line of sight from the player, named 3D nodes with `localRotDeg` and `localPos`, behaviour-graph variables, and with `scripts` the attached Papyrus scripts with their state and variables |
| World.models | main | contains?, radius? (1500, ≤ 20000), limit? (40, ≤ 300) | References in range whose model file name contains `contains` (case-insensitive), nearest first, statics included: `{total, refs:[{refID, type, name, model, distance, pos, angleDeg}]}` |
| Setting.get / Setting.set | main | name, value (set) | An INI setting (`name:Section`, e.g. `fAutoVanityModeDelay:Camera`) or a game setting (`fJumpHeightMin`). `set` handles bool, float and integer settings and returns `{name, previous, value}`; it lasts until the game closes. |
| Perf.frameStats | any | window? (seconds, default all buffered) | `{frame, count, meanMs, p50Ms, p95Ms, p99Ms, minMs, maxMs, fps, hitches, lastHitchFrame, hitchMs}` over the Present-to-Present intervals in the window. |
| Perf.hitches | any | limit? (newest, default 50), sinceFrame?, clear?, calls? | `{thresholdMs, logged, hitches[], slowCalls}`; each hitch `{frame, epochMs, agoSec, ms, threadCpuMs, threadKernelMs, blockedMs, prevPresentMs, pageFaults, readMB, writeMB, processCpuMs, workingSetMB, availPhysMB, overlappingCalls[]}`; slowCalls maps method to `{count, totalMs, maxMs}` for main/UI-thread handlers over 5 ms (or queued over 100 ms). `Perf.configure` takes `hitchLogMs` (default 100). |
| Window.info | ui | none | Game window, client and monitor size, style, DPI awareness and swap chain `{width, height, windowed}`. |
| Perf.reset | any | | Clears the frame-time ring and the hitch count |
| Perf.configure | any | hitchMs? (0 or null is off, the default), ringSize? (64-1000000 frames, default 20000) | Returns the current settings. Changing `ringSize` clears the ring. |
| Profile.start | any | threads? (`main` default, `frame`, `all`, or comma-separated ids / name fragments), hz? (10-4000, default 1000), durationS? (max 600, default 30), suspendBudgetPct? (0-100, default 2.5, 0 = none), otherHz? (rate for threads other than the main one, default 250) | Starts the sampling profiler (see [profiling](profiling.md)); `busy` while one runs. Returns `Profile.status`. |
| Profile.stop | any | encoding | Stops and serializes the profile: `{bytes, encoding, encodedBytes, chunks, waitMs, serializeMs, encodeMs, summary}`. `encoding` is `json` (default) or `mszip`: Windows Compression API MSZIP, base64 encoded, several times smaller over a slow link; `profiling.mszip_decode` reads it. Calling it again after a stop re-encodes the same profile. Read it with `Profile.read`. |
| Profile.read | any | chunk | `{chunk, encoding, data, last}`: 4 MiB slices of the encoded profile JSON (raw addresses, unwind starts, Address Library IDs, modules); concatenate `data` in order. |
| Profile.status | any | | `{profiling, watching, watchHz, hitchMs, mainThread, frameThread, hitchesCaptured}` |
| Profile.watch | any | enabled?, hz? (default 250), hitchMs? (default 100) | Low-rate main-thread sampling kept in a ring so every slow frame keeps its stacks. |
| Profile.hitches | any | limit? (default 10), sinceFrame?, clear? | `{hitches[], frames[], modules[], status}`; each hitch `{frame, ms, startMs, stacks[], samples[[ms, stack, flags]], waitChain}`; flags 1 = the thread was not running. |
| Window.place | ui | mode | `offscreen`, `hide`, `minimize` or `restore` |

Mutations that take `loadEpoch` fail with `-32005` and the current epoch when the world was
reloaded since the client last saw it. Omit `loadEpoch` to skip the check.

Input is injected into the game's own input queue at the start of each input poll, before
ControlMap and menu dispatch. It does not use SendInput, so the game does not need focus. A
tap releases after `holdMs` (default 60). The game ignores input while the load fade is on
screen, and a menu ignores input during its opening transition of about 0.2 s, so wait for
`Fader Menu` to close and give a fresh menu a moment before sending keys.

Event types are `menuOpenClose {menu, opening}`, `loadGame {success, name, loadEpoch}`,
`cellChange {formId, name, editorId, entered}`, `console {line, lineSeq}` and `frameHitch {frame, ms, thresholdMs}`
(only while `Perf.configure` set a `hitchMs` threshold). `hudNotification {text, seq}` and `subtitle {speaker, text}` are pushed to subscribers.

The frame hook is a MinHook detour on `IDXGISwapChain::Present` of the renderer's main swap chain.
When no capture is pending it reads a clock, appends one sample to a ring and loads one atomic.

### Logs, game events and liveness

| Method | Thread | Params | Result and meaning |
| --- | --- | --- | --- |
| Papyrus.log | any | since?, level?, limit?, contains? | Lines the script VM logged, `{lines:[{seq, ms, severity, text}], lastSeq, dropped, counts, hooked}`. Taken from the VM's own log event; severity is info, warning, error or fatal. |
| Papyrus.stats | main | | `{runningStacks, suspendedStacks, functionMessages, detachedObjects, activeStacks, overstressed, logCounts}` |
| Logs.tail | any | source (`papyrus`, `skse`, `plugin`, `rpc`), lines? | Last lines of the file, `{lines, truncated, origin}` |
| Health.heartbeat | any | | `{uptimeMs, main:{stalledMs, lastLatencyMs}, ui:{...}}`. Answered by the connection thread; `stalledMs` is how long an unanswered probe has waited on that game thread, so a frozen thread shows as a growing number. |
| GameEvents.configure | any | containerPlayerOnly? | `containerChanged` is limited to moves involving the player by default |
| GameEvents.counts | any | | `{type:{seen, emitted}}` per game event, counting events nobody subscribed to |

Game event types: `combat {actor, target, state}`, `death {victim, killer, dead}`,
`hit {aggressor, target, source, projectile, flags}`, `containerChanged {item, count, from, to}`,
`questStage {quest, stage}`, `activate {activator, target}`, `equip {actor, item, equipped}`,
`levelUp {level}`, `spellCast {caster, spell}`, `magicEffectApply {target, caster, effect}` and
`papyrus {seq, severity, text}`. References are `{id, base?, name?}`. Payloads are built only
while the type is subscribed.

The host enables Papyrus logging, tracing and debug information in the per-run Skyrim.ini and
removes earlier Papyrus logs. A crashed, forced or unexpectedly exited run gets
`runs/<session>/evidence/` (Papyrus, SKSE, plugin and RPC tails plus Windows crash events);
`host.ps1 evidence` prints a summary, and state.json carries `hung` and `evidence`.
The isolated profile also carries the CrashLoggerSSE plugin (with its `msdia140.dll`), the one other
DLL the host accepts. Its `crash-<time>.log` files land in the profile's `SKSE` folder on the host and are
never copied off it; `host.ps1 crashes` lists the newest five as JSON (`name`, `time`, `exception`,
`topFrames` as module+offset with the Address Library id, no instruction bytes or register values), the
evidence summary carries `crashLogs`, and `game_status`/`game_stop` put the same frames under `crash.log`.
The plugin's own watchdog adds hang evidence: when the main thread runs no queued task for 6 s it
logs `main thread has not run a queued task ... frames: module+offset ...` to the plugin log (`Logs.tail
source=plugin`), at most six times per run.
`host.ps1 stop <session> -Grace N` asks the game to quit and kills it after N seconds (default 12);
state.json then carries `stopPath`: `graceful`, `killed-after-Ns` or `closing-crashed`.
`host.ps1 reset` ends the `SkyrimDevTools` scheduled task and removes the `active` lease marker when no game
process runs (with `-Force` it first kills a leftover game or loader). The Python host calls it after every stop,
with `-Force` when the session never ended, and `host.ps1 start` runs the same cleanup itself for a lease older
than 90 s, listing what it removed as `staleCleared` in its reply. `host.ps1 health` prints the game process as
JSON (`threads`, `handles`, `privateBytes`, `workingSetBytes`, `cpuSeconds`, `cpuCores`, `responding`,
`uptimeSeconds`), read from the operating system so it works while the main thread is hung.

### Dialogue records and coverage

| Method | Thread | Params | Result and meaning |
| --- | --- | --- | --- |
| Dialogue.topics | main | refID, includeGeneric? (false), onlyPassing? (false), plugins?, quest? (substring of editor ID or name), kinds? (`player`, `command`, `scene`, `combat`, `favors`, `detection`, `service`, `misc`), limit? (300), responses? (true) | Topics the actor could say or offer: `{speaker, topicTotal, infoTotal, passing, saidOnce, truncated, topics:[{formID, editorID, text, kind, subtype, plugin, quest:{formID, editorID, name, plugin, running}, offeredNow, infos:[{formID, plugin, passes, sayOnce, said, addressed, failing?, questFailing?, text?}]}]}`. Conditions are evaluated by the engine with the actor as subject and the player as target; `failing` lists the function indexes of false items. Lines whose info or quest conditions name another speaker are left out; `includeGeneric` adds lines with no speaker condition. |
| Dialogue.offered | ui | | `{open, currentInfo, topics:[{index, text, neverSaid, topic, info, quest}]}`: the dialogue menu's list with record ids. |
| Coverage.start | main | plugins? (plugin file names, case-insensitive) | Resets and starts a session; `{started, plugins, unknownPlugins, infosAlreadySaid, papyrusHooked}`. |
| Coverage.report | main | limit? (100), stop? | `{active, plugins, durationS, stages:{hit, defined, covered, quests, missed}, infos:{said, defined, covered, missed}, scenes:{started, defined}, questsStarted, papyrus:{errors, errorCount, warnings, warningCount, scriptsLogging}}`, filtered to the plugins by the plugin that created each record. |

Events: `dialogueInfo {info, topic, topicText, quest, questEditorID, plugin}` when an NPC begins a dialogue info
and `papyrusError {severity, text, script, form}` for each Papyrus warning, error or fatal log line.

### Movement, camera, navigation and recording

Headings are degrees clockwise from north (+Y), so 90 is east (+X). Distances are game units
(about 1.4 cm). Movement is driven from the input poll hook: the same injected controls a player
would press, so animation, collision, stamina and encumbrance all apply.

| Method | Thread | Params | Result and meaning |
| --- | --- | --- | --- |
| Move.walk | main (deferred) | direction? (`forward`, `back`, `left`, `right`), durationMs? (≤ 60000) or distance? (≤ 20000), run?, sprint?, stuck? | Holds the movement control until the time or the distance is used up. `{distance, displacement, durationMs, start, end, stoppedByLimit, speed?}`; distance is the path actually walked, so `speed {steadyUnitsPerSecond, peakUnitsPerSecond, walkPercent, samples}` is the median per-poll horizontal speed after the first 0.9 s (acceleration and the engine adopting a new walk speed), which an obstacle in the last stretch does not change the way it changes `distance`; it is what speed comparisons should use. |

| Move.turnTo | main (deferred) | headingDeg or x, y or refID; smooth? (true), degPerSec? (180) | Turns the player; replies when done with `{headingDeg, pitchDeg}` |
| Move.jump / Move.stop | main | | Taps Jump / stops a running walk or turn |
| Move.sneak | main (deferred) | on | Toggles sneak until `IsSneaking` matches; `{sneaking}` |
| Camera.state | main | | `{mode, stateId, pos, yawDeg, pitchDeg, worldFov, firstPersonFov, zoom, targetZoom, freeRotationDeg, player:{headingDeg, pitchDeg}}`; mode is `firstPerson`, `thirdPerson`, `free`, `furniture`, ... |
| Camera.set | main | mode? (`first`, `third`), yawDeg?, pitchDeg?, zoom? (0-1, third person), fov? (5-150) | Applies the given fields in that order; read Camera.state a frame later for the result |
| Camera.lookAt | main | refID or x, y, z | Turns the player's view at a reference (actors at chest height, other references at the centre of their world bound) or a point |
| Nav.plan | main | refID or x, y, z; from?; route? (`local`, `global`) | Path over the loaded navmeshes, `{found, complete, goalOnMesh, startLevelZ, goalLevelZ, points, triangles, meshes, expanded, ms}`; an incomplete path adds `blockers` (see Nav.status). With `route: "global"` it is the local path to the first gate plus `global {available, world, file, found, cost, length, expanded, startLinks, goalLinks, ms, note, points}` from the worldspace graph (see below). For debugging Nav.goto. |
| Nav.goto | main | goal, mode? (`input`), timeoutS? (300), run? (true), sprint?, onCombat? (`flee`, `fight`, `stop`, `ignore`), onDialogue? (`close`, `stop`, `wait`), onMessageBox? (`firstButton`, `stop`, `wait`), onMenu? (`close`, `stop`, `wait`), invulnerable? (false), via? ([[x,y,z]...]), route? (`global`, `local` or a recording name), routeSpacing? (600), activate? (false), fast? (false), stuck? | Starts a background navigation and replies at once with `{id, mode, goal, goalPos, straightDistance, route, globalLength?, globalNote?}`. Goals: `{refID}`, `{x, y, z, worldspace?}`, `{cell}` (editor id), `{place}` (a location's displayed name or form id, see Nav.places) `{questTarget: <quest>}` (the reference Quest.targets reports for the quest; empty string takes any running quest) or `{near: refID, radius}`. `activate` walks up to activation reach (radius 90 unless given) and activates the goal reference on arrival. Loading screens do not count against `timeoutS`; Nav.status reports `loading` and `loadingS`. A door just arrived through is not taken back (Nav.status `lastDoor`), the way out of an interior is used when no door leads to the goal, and after a door into an exterior a far goal takes the global route. When a door, gate or web within 2500 units changes open/locked/disabled state, dead ends and traps are forgotten and the route is planned again (event `navReplan`, Nav.status `openStateResets`). With `activate` on a load door the trip ends `arrived` only after the destination cell has loaded (the activation is repeated up to 3 times, then it fails). When the loaded navmeshes end at a shut way (a dead end or a stuck verdict in `input` mode), up to 4 levers, chains, switches or activators linked to a nearby gate, portcullis or drawbridge are walked to and pulled, then the route is planned again (`navReplan` with `why: leverPulled`, Nav.status `leversPulled`); torches and other loose fixtures are never taken as blockers or levers. Fails with `-32009` while one runs. `fast` (true, or a walk speed percent from 100 to 400; true is 300) raises the player walk speed (see Game.setSpeed) while the walk is undisturbed (state `moving`, no interruption, no combat) and puts it back on arrival, failure, cancel, combat, dialogue, door and loading, and ahead of every sharp corner (the player overshoots one at speed and the route is replanned); lookahead, bend rounding and the off-route limit widen with the measured speed; Nav.status reports `fast`. |
| Nav.status | main | | `{active: true, nav:{id, state, mode, goal, elapsedS, distanceRemaining, etaS, waypoint, stuckCount, replans, travelled, combat, interruption, interruptions:{count, recent}, stalls:{count, totalS, offPathCount, offPathTotalS, offPathNow, recent}, ...}}`, or `{active: false, last}` whose `state` is the outcome (`arrived`, `failed`, `cancelled`) with a `reason`. A dead end also sets `blockers {stop, gap:{horizontal, vertical}, refs:[{refID, name, model, type, state, distance, fromStop, offLine, ...}], likely}`: doors, gates, webs, levers and other activators near the stop or between it and the goal, and a one-line guess at the cause (a heuristic; confirm with World.inspect). |
| Nav.cancel | main | | Stops the navigation and releases controls |
| Nav.places | main | contains? (substring, case-sensitive), limit? (50) | Locations with a world map marker: `{count, places:[{name, location, marker, pos, world, distance}]}`. Names are in the game's language; `{place}` goals accept the name or the location id. |
| Nav.nearby | main | radius? (3000), kind? (`any`, `actor`, `door`, `object`), limit? (30) | Named references around the player, nearest first: `{count, refs:[{refID, name, pos, distance, kind, loadDoor?}]}` |
| Record.start | main | hz? (1-60, default 20), fields? (`player`, `camera`, `state`, `input`, `perf`), name? | Samples on the input poll into `runs/<session>/recordings/<name>.jsonl` |
| Record.stop / Record.status | main | | `{name, file, path, samples, durationS, distance, maxSpeed, hz}` |
| Record.list | any | | `{dir, recordings:[{name, file, bytes, path}]}` |
| Record.replayCamera | main | name, speed? (1), method? (`free`, `player`) | Flies the free camera along the recorded camera path (or puts the player on the recorded positions). Ends with `replayFinished`. |
| Record.replayInput | main | name, teleport? (true) | Best effort: moves the player to the first sample, then replays heading and held controls at the recorded times. Physics, frame timing and AI are not deterministic, so the end drifts; the result reports `endError`. |
| Record.stopReplay | main | | Stops either replay |

A recording line is one JSON object: `t`, `frame`, `gameTime`, `vel`, `teleport`, and per field
group `pos`, `rot` [heading, pitch], `speed`, `cell`, `world` (player); `cam {pos, yaw, pitch,
mode, fov}` (camera); `running`, `sprinting`, `sneaking`, `swimming`, `midair`, `combat`,
`driver`, `nav`, `stuck` (state); `input` (held control names); `frameMs` (perf).

Navigation events carry the Nav.status fields: `navProgress` (once a second), `navStuck` (plus the
recovery `attempt`), `navArrived` and `navFailed` (with `reason`). `navInterrupted {id, reason,
details, action, pos}` and `navResumed {id, after}` bracket every interruption; `navStall {id, kind,
atS, durationS, pos, cause | maxDistance}` reports each stall or off-path episode as it ends. `navRoute {id, why, global}` reports each global route (`why`: `start`, `offRoute`, `teleport`). Recording events: `recordStopped`, `replayFinished`.

**Navigation modes.** `input` is Baritone-like. It runs A* over the triangles of every loaded
navmesh, with links across mesh borders through navmesh portals and preferred triangles made
cheaper. A funnel pass turns the corridor into waypoints. The player is steered with injected
Forward and a heading set per frame. When the goal is outside the loaded cells, it walks the
best partial path and replans as new cells stream in. It also replans after deviating more than
300 units. Stuck means under 40 units moved in 2 s; the recovery ladder is activate the nearest
door, jump forward, strafe left, strafe right, back up and replan. It gives up after 8 attempts
without progress or 5 dead ends. A goal in another cell or worldspace walks to a load door
leading there (one hop) and activates it. `ai` sets the player AI-driven and calls Papyrus
`PathToReference` on a marker (or the goal ref), so the engine's own pathfinder and its
cross-cell routes drive the player. A leg that ends away from the goal is redispatched; six legs
in a row without 1500 units of progress fail the trip.

**Global routes.** The loaded navmeshes reach only a few cells, so a long trip needs a coarse
map of the whole worldspace. `skyrim-devtools-navgraph build --data <Data> --world Tamriel -o
<scratch>/navgraph/Tamriel.navg` reads the user's own installed plugins (Skyrim.esm, its masters
and Creation Club plugins, later plugins overriding earlier) and writes a graph: nodes are gates
(stretches of border where two cells' navmeshes join through portal edges), edges are walking
costs across one navmesh (Dijkstra through triangle edge midpoints, preferred triangles cheaper).
The plugin looks for `<World>.navg` in the `navgraph` folder beside its run directories, or in
`SKYRIM_DEVTOOLS_NAVGRAPH`, and reloads it when the file changes. The file is game-derived: keep
it on the machine that made it. With a graph present, an `input` trip without `via` to a goal in
the same worldspace more than 8000 units away (plan view) goes global by default; `route:
"global"` forces it (failing if no graph or route) and `route: "local"` opts out. The start joins
the graph through the loaded navmeshes (A* costs to nearby gates), the goal by straight lines,
and A* picks the gates; they become via points (accepted within 400 units) walked one at a time
by the local planner, with all interruption, watchdog and stall handling unchanged. Gates
already passed are skipped, a dead end skips the current gate, and the route is recomputed after
a teleport or when the player is more than 3000 units off it (at most every 15 s). Nav.status
adds `global {length, routes, skips}`; `distanceRemaining` follows the route.

**Interruptions.** Every poll checks, most severe first: death (fails the trip), loading or fader
screens, a paused game, Dialogue Menu, MessageBoxMenu, other blocking menus (lockpicking, book,
sleep/wait, container, barter and the like), knockdown or ragdoll, paralysis, and combat (in
combat, or hit in the last 4 s). Each is handled by its policy: `wait`; `close` (dialogue: Cancel,
then hiding the menu, then the last topic for a forced greeting that will not let go; other menus:
Cancel, then hiding); `firstButton` (selects option 0, then Accept, then hides); combat `flee`
(keep to the route by input at a sprint; engine legs stop in combat), `fight` (draw, close in and
attack the nearest enemy, then carry on) or `ignore`; `stop` fails with the reason. When the
interruption clears the trip resumes from wherever the player is, replanning or dispatching a
new engine leg. The stuck detector (below) covers
every way of getting nowhere; as a last resort on a global route, a trip stuck by displacement puts the
player back on its own trail at least 300 units behind, up to 10 times a trip; each is a `navStuck` with
`action: "unstick"` and counted in `unsticks`. Caught
again at the same spot on a global route, it also skips the current gate. `invulnerable: true` turns god mode on for the trip and restores it afterwards.

**Clearance and levels.** Local paths keep clear of walls: a navmesh portal end that touches a wall
(a vertex on a boundary edge) is pulled in by 55 units before string-pulling, and a portal that would be
left with nothing (a passage under 110 units wide) collapses to its midpoint. Passages under 160 units
wide also get lead-in points 80 units either side of the middle on the line perpendicular to the opening,
so they are entered straight. Steering aims at a sharp corner (over 25 degrees) instead of past it,
reaches sharp corners within 35 units (gentle bends within 90), and does not sprint within 220 units of a
turn over 45 degrees. A door goal (a load door chosen to change cells, or an `activate` goal on a door) is
approached through a staging point 130 units in front of it on the navmesh, on the side the player comes
from; at that point the player faces the door and activates it (reach 160 units) instead of walking into
it, and the last 200 units to a door are walked, not run. Levels: the start and goal triangle is the one
containing the point in plan view whose plane is nearest the actor's height (so a bridge goal is the deck
and standing under it is the ground), waypoints and goals count as reached only on the same level (120 and
150 units of height), and a player more than 150 units off the height of the segment it is following
replans. `tests/live/nav_clearance.py` measures stall time per pass for doors, a bridge, stairs and a tunnel.

**Stuck detector.** One detector (native/stuck.h) serves `Move.walk` and `Nav.goto` (both modes);
it is skipped during loading screens, menus, dialogue and interruptions. It reports four reasons:
`stuck` (while input is held, displacement under `distance` units over `windowS`), `noProgress` (the
best distance to the goal improved by less than `progressGain` in `progressWindowS`, even though the
player moves), `oscillation` (goal-less `Move.walk`: net displacement under `oscRatio` of the path
walked over `oscWindowS`, or the same 100-unit cell entered 4 times) and `doorLoop` (arrival through the
same load door `doorRepeat` times within `doorWindowS`). Escalation for `stuck`: step back and strafe,
jump, then replan; after more than `maxFailures` recoveries, or `totalS` in one episode, the command
stops. The other reasons replan once and stop on the next detection (a trip with no progress ends within about 20 s: the progress clock is not restarted by a recovery). A replan in `Nav.goto` records the
spot, skips the gate or waypoint whose corridor runs through it and refuses a looped-through door (and
its partner) for the rest of the trip; the local navmesh planner has no per-edge avoidance. `Move.walk`
has no route, so its replan step is a longer step back. A walk ends with error `-32011` message
`stuck` and `data` `{reason, failures, stuckS, pos, facingDeg, blockers, screenshotHint, direction,
distance, displacement, durationMs, start}`; a trip ends `failed` with `reason` `stuck (<why>): ...`,
and Nav.status `stuck` holds the same report (null otherwise). Options go in a `stuck` member of the
request: `false` turns detection off, or an object with `distance` (40), `windowS` (3), `maxFailures`
(3), `totalS` (20), `progressWindowS` (10), `progressGain` (100), `oscWindowS` (10), `oscRatio` (0.2),
`oscMinPath` (400), `doorRepeat` (2), `doorWindowS` (60). Events: `moveStuck {source: walk|nav, reason,
step, failures, stuckS, pos, facingDeg, blockers, door?}` at every detection, and `moveUnstuck {source,
failures, stuckS, pos}` when the player gets going again (250 units clear of a `stuck` spot, or a full
window with no further detection for the other reasons). `skyrim-use` writes both into the timeline JSONL
as `{"t":"event","type":...}`.

**Plan levels.** `Nav.plan` reports `startLevelZ` and `goalLevelZ`, the height of the navmesh plane the start
and goal resolved to at their x, y (null off the mesh): a goal on a bridge deck resolves to the deck, and one
over a floor with no navmesh at its height resolves to the floor or nothing, never to the wrong level
silently. `tests/live/nav_levels.py` checks this in Embershard Mine.

**Stalls.** A stall is speed under 40 units/s over 0.5 s (ending above 80); off-path, in input
mode, is more than 120 units from the planned segment (ending under 60). Each episode gets a
cause guess: engine leg ended, recovering, replan latency, actor in the way, door, turning
(waypoint overshoot), steep slope or obstacle. Input mode steers by pure pursuit 150 units along
the plan, accepts waypoints within 90 units, sprints only within 35 degrees of the heading and
replans before the end of a partial plan. `skyrim-devtools-trajectory plot --episodes` marks the
episodes on the plot, and `--route` overlays a saved Nav.plan reply or navRoute events.

**Time scale.** `sgtm <n>` (console) speeds up the whole simulation, movement included, and
navigation times scale with it; `etaS` and `elapsedS` are real seconds. Values above about 4
make collisions and streaming less reliable. `set timescale to <n>` only changes the game clock.

### Combat

Actions go through the same controls a keyboard presses (`Right Attack/Block`, `Left Attack/Block`),
so stamina, animation, timing and spell costs all apply; only equipping and turning to face a target
call into the engine. The chosen target is remembered between calls.

| Method | Thread | Params | Result and meaning |
| --- | --- | --- | --- |
| Combat.state | main | radius? (3000), limit? (10) | `{player:{health,magicka,stamina:{cur,max,pct}, dead, inCombat, weapon, drawn, blocking, attack, rightHand, leftHand}, hostileTotal, hostiles:[{refID, name, distance, bearingDeg (relative to facing, positive right), dz, level, health, inCombat, targetsPlayer, attack, blocking, chosen?}], target?}`. Hostile means alive, loaded, targeting the player or hostile to the player's factions. |
| Combat.target | main | refID?, radius?, face? (true), clear? | Picks the nearest hostile (or refID), remembers it and turns to it. |
| Combat.face | main | refID? | Yaw and pitch towards the chosen target, refID or nearest hostile. |
| Combat.draw | main | on? (true) | Draws or sheathes weapon and hands. |
| Combat.equip | main | name or formID or editorID, hand? (`right`, `left`, `both`) | Equips a weapon, shield, armor, ammo or spell (inventory or spell list match by display name). Applies shortly after the reply. |
| Combat.unequip | main | hand? | Takes a weapon or shield out of a hand. |
| Combat.attack | main (deferred) | kind? (`light`, `power`), hand?, face? (true), refID?, holdMs? | Holds the attack control (light 120 ms, power 900 ms) and replies after the release. |
| Combat.block | main (deferred) | holdMs? (1500), face?, refID? | Holds the left-hand control. |
| Combat.cast | main (deferred) | hand?, holdMs? (concentration 2000, otherwise 250), face?, refID? | Casts the spell in that hand; refuses a spell the magicka cannot pay for. |
| Combat.drink | main | kind? (`health`, `magicka`, `stamina`) or name or formID | Drinks the weakest potion that covers the missing amount, else the strongest; skips poisons and food. |
| Ranged.loadout | main | equip? (true), spell? | The best bow (or crossbow) and matching ammunition in the inventory, equipped unless `equip: false`: `{weapon, ready, missing[], bow{formID,name,speed,crossbow,damage,reach}, ammo{formID,name,kind,count,damage,projectile{speed,gravity,range}}, bowEquipped, ammoEquipped, equipQueued}`. With `spell: true` it describes the projectile spell in the right hand instead (`spell`, `projectile`, `concentration`, `cost`, `magicka`). |
| Ranged.los | main | refID, part? (`torso`, `head`), from? [x,y,z], points? [[x,y,z]...] | Engine ray (collision layer LOS) from the player's eye, or `from`, to the target's torso or head: `{clear, hitDistance?, hitLayer?, hitPoint?, from, torso, head, target, distance, health, dead, points?:[{clear, hitDistance?, distance}]}`. An actor hit within the last tenth of the line counts as the target. `points` are candidate standing positions (feet; the eye height is added) tested against the same aim point. |
| Ranged.aim | main | headingDeg, pitchDeg | Absolute aim: heading clockwise from north, pitch in degrees above the horizon (clamped to +-80). Replies with the attack and weapon state ints and the eye position. |
| Ranged.arrows | main | | The player's own projectiles in the manager: `{arrows:[{refID, pos, velocity, distance, power, destroyed}]}` (velocity in units per second), for flight tracking and calibration. |
| Ranged.state | main | | `{attackState, weaponState, drawn, eye, position, headingDeg, ammoLeft?}`. |

### Alerts

| Method | Thread | Params | Result |
|---|---|---|---|
| Alerts.state | any | history? (bool), limit? (history entries, default 50, max 200) | `{active[], history?[], cost, thresholds, disabled[]}`; an alert is `{kind, severity (info\|warn\|error), sinceMs, ageS, hint, detail}`; a history entry `{action: raised\|cleared, ms, kind, ...}`; `cost` is `{ticks, mainMeanUs, mainMaxUs, mainLastUs, pollerMeanUs, pollerMaxUs}` |
| Alerts.configure | any | thresholds? (name to number), disable? / enable? (kind lists), reset? | Current `{thresholds, disabled}`. Names: pollMs, questStalledMin, combatStalemateS, deathCount, deathWindowMin, deathNearDist, fallDrop, fallTimeS, swimStallS, swimProgressDist, pitchLimitDeg, pitchBlindS, menuOpenS, notFocusedS, lowFpsBelow, lowFpsS, hitchMs, lowHealthPct, targetCheckS, questCheckS, healCheckS |
| Alerts.dismiss | any | kind, forS? (default 300) | Clears the alert and suppresses re-raising it for that long |

Events: `alertRaised {kind, severity, sinceMs, ageS, hint, detail}` and `alertCleared {kind, sinceMs, durationMs, why}`.
Kinds the plugin raises: questStalled, combatStalemate, deathLoop, fellOrOutOfBounds, blindCamera (pitch),
menuLeftOpen, paused, lowFps, overEncumbered, crime, targetDead, targetUnreachable, lowHealthNoHeal; the skyrim-use
server adds actionLoop, a screenshot-based blindCamera and moveStuck (from `moveStuck`/`moveUnstuck`).
`Screen.capture` replies also carry `meanLuma` (0-255) and `dominantShare` of the encoded frame. See
docs/skyrim-use.md, Alerts.

## Errors

| Code | Meaning |
| --- | --- |
| -32700 / -32600 / -32601 / -32602 / -32603 | Parse error, invalid request, unknown method, bad params, internal error |
| -32004 | The game is not ready (UI not up yet, or not playable) |
| -32005 | Stale load epoch |
| -32006 | Input hook unavailable |
| -32007 | Console unavailable |
| -32008 | Timed out waiting for the game; `data` names the method, thread and timeout |
| -32009 | Too many calls in flight |
| -32010 | Not found (unknown menu or save) |
| -32011 | Stuck: `Move.walk` stopped by the stuck detector; `data` has reason, pos, facingDeg, blockers |

## Adding a method

Add a `.cpp` file to `native/`. CMake picks it up automatically, and a static registrar
installs it at startup:

~~~cpp
#include "methods.h"
DEVTOOLS_MODULE(Example)
{
    devtools::Register("Example.echo", [](const devtools::Json& p) { return p; }, { .thread = devtools::Thread::Main });
}
~~~

Use `AllowOnly`, `Require<T>` and `Optional<T>` from `core.h` to validate params, and throw
`RpcError` for failures. Use `RegisterDeferred` when the reply comes later, from an event
sink or another thread. Handlers on `Thread::Any` run on the connection thread and must
not touch game state.

## Adding an event

~~~cpp
#include "events.h"
DEVTOOLS_MODULE(Example)
{
    devtools::events::Declare("exampleThing", "Something happened: {value}");
}
// from any sink or thread:
if (devtools::events::Subscribed("exampleThing")) devtools::events::Emit("exampleThing", { { "value", 1 } });
~~~

Register game event sinks from an `OnSkseMessage` listener at `kDataLoaded`; see `world.cpp`.
