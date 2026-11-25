# skyrim-use: playtest Skyrim from Claude

`skyrim-use` is an MCP server (stdio) that drives the DevTools plugin described in
[protocol.md](protocol.md). It starts the game on the Windows host, keeps one session alive across tool
calls, and exposes menus, console, input, state queries and screenshots as tools an LLM can call.

## Install and register

~~~sh
pip install -e .            # installs the `skyrim-use` command (needs Python 3.13)
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
  that as `unrequested-exit`. `game_stop` closes open gameplay menus (shutting down with the inventory open crashed the game), stops through the host and reports `cleanExit`.

## Tools

| Group | Tools |
| --- | --- |
| Lifecycle | `game_start {headless=true, save?}`, `game_stop`, `game_status` |
| Observe | `snapshot`, `list_menus {open_only}`, `events_list`, `events_subscribe`, `events_drain {types?, limit}`, `wait_for` |
| Act | `open_menu`, `close_menu`, `console {command, target}`, `press_key`, `press_control`, `mouse`, `release_all_input`, `look {yaw_deg, pitch_deg}` |
| Saves and world | `list_saves`, `load_save`, `reload`, `center_on_cell` |
| State (newer plugin builds) | `player_state`, `player_inventory`, `world_nearby`, `world_crosshair`, `quest_get`, `quest_active`, `message_box`, `message_box_select`, `dialogue_state`, `dialogue_select`, `ui_list_items`, `ui_select_item`, `screenshot` (image), `perf_frame_stats`, `papyrus_log` |
| Generic | `rpc_methods`, `rpc_call {method, params}` |

The state tools wrap native methods that older plugin builds lack. They answer "method X is not available in
this plugin build" instead of failing obscurely, and `rpc_call` reaches any method before a wrapper exists.

`wait_for` blocks until all given conditions hold: `menu_open`, `menu_closed`, `playable`, `event_type`
(a buffered, not yet drained event; it does not consume it) and a generic probe `method` + `path` + `equals`,
for example `method=Player.state path=location.cell equals=WhiterunBanneredMare`.

`events_drain` returns compact `{type, seq, epoch, data}` records since the previous drain. After
`game_start` every declared event is buffered except `console` (the `console` tool already returns its lines).

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
