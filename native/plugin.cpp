// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Plugin entry: reads the host's environment, installs the modules and starts the transport.
// Methods and events live in their own modules; see docs/protocol.md.
#include "bus.h"
#include "events.h"
#include "game.h"
#include "methods.h"
#include "server/server.h"

#include <MinHook.h>
#include <Windows.h>

#include <array>
#include <charconv>

namespace
{
    constexpr char imageSha[] = "846efccf0c1374d71f892907f46549560f2fcb0a75cb87a3eed438baa0f1402f";

    std::string Environment(const char* name)
    {
        std::array<char, 256> buffer{};
        const auto size = GetEnvironmentVariableA(name, buffer.data(), static_cast<DWORD>(buffer.size()));
        return size && size < buffer.size() ? std::string(buffer.data(), size) : std::string{};
    }

    std::filesystem::path EnvironmentPath(const wchar_t* name)
    {
        std::array<wchar_t, 32768> buffer{};
        const auto size = GetEnvironmentVariableW(name, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (!size || size >= buffer.size()) {
            return {};
        }
        return std::filesystem::path(std::wstring(buffer.data(), size));
    }

    // Absent values take the fallback; malformed values reject the whole launch.
    std::optional<int> Integer(const char* name, int fallback, int low, int high)
    {
        const auto text = Environment(name);
        if (text.empty()) {
            return fallback;
        }
        int value = 0;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
        if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || value < low || value > high) {
            return std::nullopt;
        }
        return value;
    }

    void RequestClose()
    {
        if (const auto window = devtools::MainWindow()) {
            PostMessageW(window, WM_CLOSE, 0, 0);
        }
    }

    void OnMessage(SKSE::MessagingInterface::Message* message)
    {
        if (message) {
            devtools::DispatchSkseMessage(*message);
        }
    }
}

SKSE_PLUGIN_VERSION = [] {
    SKSE::PluginVersionData version{};
    version.PluginVersion(REL::Version{ 2, 0, 0, 0 });
    version.PluginName("SkyrimDevTools");
    version.CompatibleVersions({ SKSE::RUNTIME_SSE_1_7_104 });
    return version;
}();

SKSE_PLUGIN_LOAD(const SKSE::LoadInterface* skse)
{
    if (skse->RuntimeVersion() != SKSE::RUNTIME_SSE_1_7_104 ||
        skse->SKSEVersion() != REL::Version{ 2, 3, 1, 0 }.pack()) {
        return false;
    }

    std::string token = Environment("SKYRIM_DEVTOOLS_TOKEN");
    auto& config = devtools::GetConfig();
    config.sessionId = Environment("SKYRIM_DEVTOOLS_SESSION");
    // A game started without the host stays a plain game.
    if (token.empty() && config.sessionId.empty()) {
        return true;
    }
    config.build = "sse-steam-1-7-104";
    config.imageSha256 = imageSha;
    config.headless = Environment("SKYRIM_DEVTOOLS_HEADLESS") == "1";
    config.savesDir = EnvironmentPath(L"SKYRIM_DEVTOOLS_SAVES_DIR");
    const auto port = Integer("SKYRIM_DEVTOOLS_PORT", 0, 1024, 65535);
    const auto idle = Integer("SKYRIM_DEVTOOLS_IDLE_TIMEOUT", 1800, 0, 7 * 24 * 3600);
    const auto connect = Integer("SKYRIM_DEVTOOLS_CONNECT_TIMEOUT", 600, 0, 7 * 24 * 3600);
    const auto runDir = EnvironmentPath(L"SKYRIM_DEVTOOLS_RUN_DIR");
    if (!port || !idle || !connect || token.size() != 64 || config.sessionId.size() != 32 || runDir.empty() ||
        Environment("SKYRIM_DEVTOOLS_IMAGE") != imageSha) {
        return false;
    }

    SKSE::Init(skse, SKSE::InitInfo{ .log = false });
    const auto messages = SKSE::GetMessagingInterface();
    if (!messages || !SKSE::GetTaskInterface() || !messages->RegisterListener(OnMessage)) {
        return false;
    }
    const auto hooks = MH_Initialize();
    if (hooks != MH_OK && hooks != MH_ERROR_ALREADY_INITIALIZED) {
        return false;
    }
    devtools::InstallModules();

    devtools::ServerOptions options;
    options.port = *port;
    options.token = std::move(token);
    options.transcript = runDir / L"rpc.jsonl";
    options.idleTimeout = std::chrono::seconds(*idle);
    options.connectTimeout = std::chrono::seconds(*connect);
    devtools::ServerHooks server;
    server.connected = [](std::shared_ptr<devtools::Outbox> outbox) { devtools::events::Attach(std::move(outbox)); };
    server.message = [](const std::string& text, const std::shared_ptr<devtools::Outbox>& outbox) {
        devtools::bus::Handle(text, outbox);
    };
    server.disconnected = [] { devtools::events::Detach(); };
    server.closeSession = RequestClose;
    return devtools::StartServer(std::move(options), std::move(server));
}
