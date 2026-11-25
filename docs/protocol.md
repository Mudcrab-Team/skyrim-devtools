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
`loadEpoch`, `headless`, `capabilities` (`console`, `events`, `headless`, `input`, `loadSave`,
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
| Events.snapshot | ui | | `{seq, loadEpoch, dataLoaded, loadCompleted, loadSucceeded, playable, menus:[{name, ready}], foreground, headless, lastSave}` |
| Events.list | any | | Declared event types with descriptions |
| Events.subscribe / unsubscribe | any | types (`"*"` for all) | Selects pushed event types. Resets on every connection. |
| UI.openMenu / closeMenu | ui | name, wait?, anyState?, loadEpoch? | Any registered menu. `wait` replies once the menu actually opens or closes. Unless `anyState` is set, the world must be playable. |
| UI.listMenus | ui | | `[{name, open}]` for every registered menu |
| UI.isMenuOpen | ui | name | `{menu, open, ready}` |
| Console.execute | main | command, target?, loadEpoch? | Runs any console command. `target` is `selected` (default), `player` or `none`. Returns `{command, lines}`, every line printed while it ran. |
| Console.history | any | since? | Recent console lines from any source, `{lastSeq, lines:[{line, lineSeq}]}` |
| Input.key | any | key, phase?, holdMs? | DirectInput scancode (1-255) or name (`Tab`, `E`, `Grave`, `F1` ...). `phase` is `tap` (default), `down` or `up`. |
| Input.control | any | name, phase?, holdMs? | ControlMap user event (`Journal`, `Tween Menu`, `Activate` ...), resolved to its bound key in the innermost active input context |
| Input.mouse | any | dx, dy or button, phase?, holdMs? | Relative motion, or button `left`, `right`, `middle` or 0-7 |
| Input.releaseAll / Input.stats | any | | Releases held keys / `{hooked, polls, msSinceLastPoll, injectedButtons, injectedMoves}` |
| Player.look | main | yawDeg?, pitchDeg? | Turns the player by the given amount, relative to the current view. Pitch is clamped to ±85°. |
| World.centerOnCell | main | cell, loadEpoch? | `coc <cell>`, returns the console lines |
| Screen.capture | any (deferred) | maxWidth? (1024), format? (`png` or `jpeg`), quality? (85, jpeg), includeUI? (true), path? | Reads the game's own swap-chain back buffer on the render thread just before Present, so it works headless and off-screen and never uses desktop capture. Scales down (never up) and encodes with WIC. Returns `{frame, width, height, sourceWidth, sourceHeight, format, bytes, data, readbackMs, encodeMs, totalMs}` with base64 `data`. With `path` (a plain file name) the image is written to the run's `screens/` directory and `path` replaces `data`. `includeUI: false` hides menus and the HUD for a few frames and restores them. |
| Perf.frameStats | any | window? (seconds, default all buffered) | `{frame, count, meanMs, p50Ms, p95Ms, p99Ms, minMs, maxMs, fps, hitches, lastHitchFrame, hitchMs}` over the Present-to-Present intervals in the window. |
| Perf.reset | any | | Clears the frame-time ring and the hitch count |
| Perf.configure | any | hitchMs? (0 or null is off, the default), ringSize? (64-1000000 frames, default 20000) | Returns the current settings. Changing `ringSize` clears the ring. |
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
(only while `Perf.configure` set a `hitchMs` threshold).

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
A separate crash-logging DLL would give stack traces but is not used while the plugin
stays the only SKSE DLL.

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
