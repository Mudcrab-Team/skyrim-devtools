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
`menus`), every registered method with its thread and timeout, and bus counters.

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
| Window.place | ui | mode | `offscreen`, `hide`, `minimize` or `restore` |

Mutations that take `loadEpoch` fail with `-32005` and the current epoch when the world was
reloaded since the client last saw it. Omit `loadEpoch` to skip the check.

Input is injected into the game's own input queue at the start of each input poll, before
ControlMap and menu dispatch. It does not use SendInput, so the game does not need focus. A
tap releases after `holdMs` (default 60). The game ignores input while the load fade is on
screen, and a menu ignores input during its opening transition of about 0.2 s, so wait for
`Fader Menu` to close and give a fresh menu a moment before sending keys.

Event types are `menuOpenClose {menu, opening}`, `loadGame {success, name, loadEpoch}`,
`cellChange {formId, name, editorId, entered}` and `console {line, lineSeq}`.

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
