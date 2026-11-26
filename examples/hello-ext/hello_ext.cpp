// SPDX-License-Identifier: MIT
// A minimal extension plugin: publishes two methods and one event on the SkyrimDevTools bus. When the
// devtools plugin is not loaded it does nothing, so the same DLL is safe in a normal game.
#include "skyrim_devtools_ext.h"

#include <RE/Skyrim.h>
#include <SKSE/SKSE.h>

#include <Windows.h>

#include <cstdio>
#include <string>

namespace
{
    constexpr const char* owner = "HelloExt";
    const SkyrimDevtoolsApi* api = nullptr;

    // Answers on the connection thread: no game state is touched, so THREAD_ANY is enough.
    void Echo(const char* params, SkyrimDevtoolsReply reply, void*)
    {
        // The params object is plain JSON text; a real plugin would parse it with its own JSON library.
        const std::string text = params;
        std::string quoted = "\"";
        for (const char c : text) {
            quoted += (c == '"' || c == '\\') ? std::string{ '\\', c } : std::string{ c };
        }
        quoted += '"';
        api->reply_ok(reply, ("{\"received\":" + quoted + "}").c_str());
    }

    // Reads the player, which only the main thread may do.
    void PlayerName(const char*, SkyrimDevtoolsReply reply, void*)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        const char* name = player ? player->GetName() : nullptr;
        if (!name) {
            api->reply_fail(reply, -32004, "No player yet: load a save first");
            return;
        }
        api->reply_ok(reply, (std::string("{\"name\":\"") + name + "\"}").c_str());
    }

    void EmitPing(const char*, SkyrimDevtoolsReply reply, void*)
    {
        static int count = 0;
        char data[48];
        std::snprintf(data, sizeof(data), "{\"count\":%d}", ++count);
        api->emit_event("HelloExt.ping", data);
        api->reply_ok(reply, data);
    }

    void Register()
    {
        const auto module = GetModuleHandleA("SkyrimDevTools.dll");
        const auto getApi = module ? reinterpret_cast<SkyrimDevtoolsGetApiFn>(GetProcAddress(module, "SkyrimDevtools_GetApi")) : nullptr;
        api = getApi ? getApi(SKYRIM_DEVTOOLS_API_VERSION) : nullptr;
        if (!api) {
            return;
        }
        SkyrimDevtoolsMethod echo{ sizeof(echo), "HelloExt.echo", "Returns the params it received as text (connection thread).",
            R"({"type":"object","properties":{"text":{"type":"string","description":"Anything"},"times":{"type":"integer","minimum":1}},"required":["text"],"additionalProperties":false})",
            SKYRIM_DEVTOOLS_THREAD_ANY, 0, &Echo, nullptr };
        SkyrimDevtoolsMethod name{ sizeof(name), "HelloExt.playerName", "The player character's name (main thread).",
            R"({"type":"object","properties":{},"additionalProperties":false})", SKYRIM_DEVTOOLS_THREAD_MAIN, 0, &PlayerName, nullptr };
        SkyrimDevtoolsMethod ping{ sizeof(ping), "HelloExt.emitPing", "Emits the HelloExt.ping event and returns its payload.",
            nullptr, SKYRIM_DEVTOOLS_THREAD_ANY, 0, &EmitPing, nullptr };
        api->register_method(owner, &echo);
        api->register_method(owner, &name);
        api->register_method(owner, &ping);
        api->declare_event(owner, "HelloExt.ping", "Emitted by HelloExt.emitPing: {count}");
    }
}

SKSE_PLUGIN_VERSION = [] {
    SKSE::PluginVersionData version{};
    version.PluginVersion(REL::Version{ 1, 0, 0, 0 });
    version.PluginName("HelloExt");
    version.CompatibleVersions({ SKSE::RUNTIME_SSE_1_7_104 });
    return version;
}();

SKSE_PLUGIN_LOAD(const SKSE::LoadInterface* skse)
{
    SKSE::Init(skse, SKSE::InitInfo{ .log = false });
    SKSE::GetMessagingInterface()->RegisterListener([](SKSE::MessagingInterface::Message* message) {
        // Every plugin has loaded by now, so the devtools plugin is there if it is installed at all.
        if (message && message->type == SKSE::MessagingInterface::kPostPostLoad) {
            Register();
        }
    });
    return true;
}
