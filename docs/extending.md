# Extending the bench from your own plugin

A mod under test usually has state and actions an agent cannot reach through the game's UI: an internal counter, a
debug toggle, a "spawn this scenario" function. Instead of asking agents to go through the console, the mod publishes
them on the playtesting bus. Each registered method becomes an MCP tool with the schema you wrote, and your plugin can
push events into the agent's event stream.

The interface is a small plain-C header, [`ext/skyrim_devtools_ext.h`](../ext/skyrim_devtools_ext.h) (MIT, versioned,
no dependency on this repository's code or on a JSON library). A working plugin is in
[`examples/hello-ext`](../examples/hello-ext/hello_ext.cpp).

## Get the API

Two ways, same table:

```cpp
// 1. the export (works at any time after both DLLs are loaded)
auto module = GetModuleHandleA("SkyrimDevTools.dll");
auto getApi = module ? (SkyrimDevtoolsGetApiFn)GetProcAddress(module, "SkyrimDevtools_GetApi") : nullptr;
const SkyrimDevtoolsApi* api = getApi ? getApi(SKYRIM_DEVTOOLS_API_VERSION) : nullptr;
```

```cpp
// 2. SKSE messaging: listen to sender "SkyrimDevTools"; the host broadcasts after kPostPostLoad
messaging->RegisterListener("SkyrimDevTools", [](SKSE::MessagingInterface::Message* m) {
    if (m->type == SKYRIM_DEVTOOLS_MESSAGE_API) api = static_cast<const SkyrimDevtoolsApi*>(m->data);
});
```

`nullptr` means the devtools plugin is not loaded (a normal game) or is older than the version you asked for: do nothing,
your plugin must work without it. The plugin is only active in a session started by the host; outside one, registration
calls are accepted and have no audience.

## Register a method

```cpp
SkyrimDevtoolsMethod m{ sizeof(m), "MyMod.setDifficulty", "Sets the mod's difficulty tier and returns the old one.",
    R"({"type":"object","properties":{"tier":{"type":"integer","minimum":0,"maximum":5}},"required":["tier"],
        "additionalProperties":false})",
    SKYRIM_DEVTOOLS_THREAD_MAIN, 0, &SetDifficulty, nullptr };
api->register_method("MyMod", &m);

void SetDifficulty(const char* paramsJson, SkyrimDevtoolsReply reply, void*) {
    // parse paramsJson with your own JSON library, act, then answer exactly once
    api->reply_ok(reply, R"({"previous":2})");
}
```

- **Name**: `Plugin.method`, letters, digits, `_` and `.`, at most 64 characters. A name that is a built-in method or
  belongs to another plugin is refused (`SKYRIM_DEVTOOLS_ERR_CONFLICT`); registering your own name again replaces it.
- **Threads**: `THREAD_ANY` runs on the connection thread and must not touch game state; `THREAD_MAIN` runs as an SKSE
  task, `THREAD_UI` on the UI task queue. A long-running handler on MAIN or UI stalls the game, so keep it short.
- **Answering**: `reply_ok` with a JSON value or `reply_fail` with a code and message, once. You may keep the reply
  and answer later from any thread, within `timeout_ms` (default 5000); after that the client has already received a
  timeout and a late answer is dropped. The params text is only valid until the handler returns.
- **Schema**: JSON Schema of the params *object*. It is the tool's input schema as the agent sees it, so write
  `description` fields. Use `"additionalProperties": false` so that a misspelt key is reported (below).
- Registering or removing methods after the client connected emits `methodsChanged`; the client rereads the list and
  updates its tools. `unregister_method(owner, name)` removes one (for example when your mod is disabled in-game).

## Emit events

```cpp
api->declare_event("MyMod", "MyMod.waveStarted", "A wave began: {index, enemies}");
api->emit_event("MyMod.waveStarted", R"({"index":3,"enemies":12})");
```

Event types follow the same naming rule as methods. They reach the agent through `events_drain` and `wait_for
event_type=...` like built-in events, while subscribed (the server subscribes to everything but `console` at connect).
`emit_event` is cheap and returns at once when nobody is subscribed, so call it freely from any thread.

## What the agent sees

`HelloExt.echo` becomes the MCP tool `hello_ext_echo` (CamelCase and dots become snake_case; a name that clashes with a
curated tool gets an `ext_` prefix). The tool description is yours plus the method name and owning plugin. Arguments are
the schema's top-level properties. A schema whose property names are not valid Python identifiers (for example `from`) is
exposed as one `arguments` object instead. `rpc_methods` lists every method with `owner`, `description` and `schema`, and
`rpc_call` reaches the method by name as well.

Tools appear when `game_start` returns and are removed on `game_stop`; clients that honour MCP list-changed
notifications pick up methods registered later automatically.

## Argument checking

An MCP client may send keys a tool does not know; without a check they would be dropped silently, so a typo
(`timeout` for `timeout_s`) reads as "the default was used". Every tool reply now carries

```json
"warnings": [{"code": "unknownArguments", "unknown": ["txt"], "accepted": ["text", "times"],
              "message": "tool hello_ext_echo: unknown argument(s) 'txt' ignored; accepted keys: text, times"}]
```

(appended as a `warnings: [...]` line when the reply is not a JSON object). The accepted keys come from the tool's input
schema: your method schema for extension tools, the function signature for curated ones. `rpc_call` checks the `params`
object against the method's schema when it has one. Only top-level keys are checked. In strict mode the call is refused
with an error naming the accepted keys: `game_options {changes: {strictArgs: true}}`, or `"options": {"strictArgs": true}`
in the host config.

## Options

Robustness switches for unattended sessions. Set them under `options` in the host config JSON (applied at every game
start) or live with the `game_options` tool (`Options.set` natively); live changes are kept for the next game too.

| Option | Default | Effect |
|---|---|---|
| `alwaysActive` | false | The game keeps running while its window is in the background. Sets `bAlwaysActive` and swallows the window's deactivation notices. |
| `autoDismissMenus` | [] | Menu names (`["TutorialMenu"]`) hidden shortly after they open; `menuAutoDismissed` is emitted. Message boxes and the core menus (Main, Loading, Fader, HUD, Cursor) are never dismissed and are reported under `ignored`. |
| `disableVanityCamera` | true | No idle orbit camera (`vanityCamera: true` in the host config also keeps it). Turning it off again restores the game's own delay. |
| `cleanTransition` | false | `center_on_cell` and `console coc/cow ...` first go to a small interior cell and wait for its loading screen, so the destination loads from a clean state instead of streaming over the old surroundings. Replies carry `bounced: true`. Skipped when the player is already in the bounce cell. `target=selected` is not carried across the bounce. |
| `cleanTransitionCell` | QASmoke | Editor ID of the interior cell used for the bounce; must exist and be an interior. |
| `stallThresholdMs` | 6000 | Main-thread stall threshold of the watchdog (1000 to 600000). |
| `strictArgs` | false | Unknown tool arguments are an error instead of a warning. |

The stall watchdog was already writing a stack capture to the plugin log; it now also emits `mainThreadStalled` once per
stall and `mainThreadResumed` when the thread recovers, and `game_status` shows `mainThreadStalled` while it lasts. The
process-level health readings (thread count, not responding) stay in `game_status.health`, which is the check for a hung
game when the main thread cannot answer at all.

## Build and deploy the example

`native/CMakeLists.txt` builds `SkyrimDevToolsExample.dll` next to the plugin. `tools/pc-build --deploy --with-example`
copies both DLLs into the test profile's `Data/SKSE/Plugins`. Your own plugin only needs the header on its include path.

## Compatibility

The header carries `SKYRIM_DEVTOOLS_API_VERSION` (1). A later version adds fields at the end of structures and bumps the
version; a plugin built against version 1 keeps working because the host answers `SkyrimDevtools_GetApi(1)` for as long as
it can, and every structure starts with its size.
