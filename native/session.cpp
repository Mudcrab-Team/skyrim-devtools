// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Session lifecycle, save loading and the generic state snapshot.
#include "bus.h"
#include "events.h"
#include "game.h"
#include "methods.h"

#include <Windows.h>

#include <mutex>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr int protocol = 2;

    std::mutex loadMutex;
    std::string lastSave;
    std::string pendingSave;
    ReplyPtr pendingLoadReply;  // a loadSave call with wait=true, answered on kPostLoadGame
    std::atomic<std::uint64_t> snapshotSequence = 0;

    std::string ValidSaveName(const std::string& name)
    {
        if (name.empty() || name.size() > 200 || name.find_first_of("/\\:*?\"<>|") != std::string::npos ||
            name.find("..") != std::string::npos) {
            throw RpcError(code::invalidParams, "A save name is a file name without directory or extension");
        }
        const auto& dir = GetConfig().savesDir;
        if (!dir.empty() && !std::filesystem::is_regular_file(dir / (name + ".ess"))) {
            throw RpcError(code::notFound, "No save named " + name);
        }
        return name;
    }

    void RequireIsolatedSavePath()
    {
        const auto settings = RE::INISettingCollection::GetSingleton();
        const auto setting = settings ? settings->GetSetting("sLocalSavePath:General") : nullptr;
        const auto path = setting ? setting->GetString() : nullptr;
        if (!path || _stricmp(path, "Saves\\") != 0) {
            throw RpcError(code::notReady, "The isolated save path is not active");
        }
    }

    void BeginLoad(const std::string& name, bool wait, const ReplyPtr& reply)
    {
        RequireUI();
        RequireIsolatedSavePath();
        const auto manager = RE::BGSSaveLoadManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "The save manager is unavailable");
        }
        std::uint64_t epoch = 0;
        {
            std::lock_guard lock(loadMutex);
            if (pendingLoadReply && !pendingLoadReply->Done()) {
                throw RpcError(code::busy, "Another load is still waiting for completion");
            }
            epoch = ++state::epoch;
            pendingSave = name;
            pendingLoadReply = wait ? reply : nullptr;
        }
        manager->Load(name.c_str());
        if (!wait) {
            reply->Ok({ { "loadEpoch", epoch }, { "name", name } });
        }
    }

    void OnLoadMessage(SKSE::MessagingInterface::Message& message)
    {
        if (message.type == SKSE::MessagingInterface::kPreLoadGame) {
            state::loadCompleted = false;
            state::loadSucceeded = false;
            return;
        }
        if (message.type != SKSE::MessagingInterface::kPostLoadGame) {
            return;
        }
        // The message encodes the success flag in the pointer value.
        const bool success = message.data != nullptr;
        state::loadSucceeded = success;
        state::loadCompleted = true;
        ReplyPtr reply;
        std::string name;
        {
            std::lock_guard lock(loadMutex);
            name = pendingSave;
            if (success && !name.empty()) {
                lastSave = name;
            }
            pendingSave.clear();
            reply = std::move(pendingLoadReply);
        }
        const Json data{ { "success", success }, { "name", name.empty() ? Json(nullptr) : Json(name) },
            { "loadEpoch", state::epoch.load() } };
        events::Emit("loadGame", data);
        if (reply) {
            if (success) {
                reply->Ok(data);
            } else {
                reply->Fail(code::notReady, "The game reported a failed load", data);
            }
        }
    }

    Json Snapshot()
    {
        auto& ui = RequireUI();
        auto menus = Json::array();
        for (const auto& entry : ui.menuStack) {
            if (!entry) {
                continue;
            }
            std::string name;
            for (const auto& item : ui.menuMap) {
                if (item.second.menu.get() == entry.get()) {
                    name = item.first.c_str();
                    break;
                }
            }
            menus.push_back({ { "name", name }, { "ready", entry->uiMovie && entry->uiMovie->IsAvailable("_root") } });
        }
        std::string save;
        {
            std::lock_guard lock(loadMutex);
            save = lastSave;
        }
        return { { "seq", ++snapshotSequence }, { "loadEpoch", state::epoch.load() },
            { "dataLoaded", state::dataLoaded.load() }, { "loadCompleted", state::loadCompleted.load() },
            { "loadSucceeded", state::loadSucceeded.load() }, { "playable", Playable(ui) },
            { "menus", std::move(menus) }, { "foreground", GameIsForeground() },
            { "headless", GetConfig().headless }, { "lastSave", save.empty() ? Json(nullptr) : Json(save) } };
    }
}

DEVTOOLS_MODULE(Session)
{
    events::Declare("loadGame", "A save finished loading: {success, name, loadEpoch}");
    OnSkseMessage(OnLoadMessage);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            state::dataLoaded = true;
        }
    });
    SetCapability("loadSave", true);
    SetCapability("events", true);

    Register("Session.getInfo", [](const Json& params) {
        AllowOnly(params, {});
        const auto& config = GetConfig();
        return Json{ { "protocol", protocol }, { "build", config.build }, { "imageSha256", config.imageSha256 },
            { "sessionId", config.sessionId }, { "dataLoaded", state::dataLoaded.load() },
            { "loadEpoch", state::epoch.load() }, { "headless", config.headless },
            { "capabilities", Capabilities() }, { "methods", ListMethods() }, { "bus", bus::Stats() } };
    }, { .thread = Thread::Any });
    Register("Session.ping", [](const Json& params) {
        AllowOnly(params, { "payload" });
        return Json{ { "pong", true }, { "payload", params.value("payload", Json(nullptr)) } };
    }, { .thread = Thread::Any });
    // A ping that crosses a game thread proves that thread is still turning over.
    Register("Session.pingMain", [](const Json& params) {
        AllowOnly(params, {});
        return Json{ { "pong", true } };
    }, { .thread = Thread::Main });
    Register("Session.pingUI", [](const Json& params) {
        AllowOnly(params, {});
        return Json{ { "pong", true } };
    }, { .thread = Thread::UI });
    RegisterDeferred("Session.close", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, {});
        reply->Ok({ { "closing", true } });
        events::CloseConnection();
    }, { .thread = Thread::Any });

    Register("Session.listSaves", [](const Json& params) {
        AllowOnly(params, {});
        const auto& dir = GetConfig().savesDir;
        if (dir.empty()) {
            throw RpcError(code::notReady, "The host did not name the saves directory");
        }
        auto saves = Json::array();
        for (const auto& entry : std::filesystem::directory_iterator(dir)) {
            if (entry.is_regular_file() && _wcsicmp(entry.path().extension().c_str(), L".ess") == 0) {
                saves.push_back({ { "name", entry.path().stem().string() }, { "bytes", entry.file_size() } });
            }
        }
        return saves;
    }, { .thread = Thread::Any });
    RegisterDeferred("Session.loadSave", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "name", "wait" });
        BeginLoad(ValidSaveName(Require<std::string>(params, "name")), Optional(params, "wait", false), reply);
    }, { .thread = Thread::Main, .timeout = 180s });
    RegisterDeferred("Session.reload", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "wait" });
        std::string name;
        {
            std::lock_guard lock(loadMutex);
            name = lastSave;
        }
        if (name.empty()) {
            throw RpcError(code::notReady, "No save has been loaded through Session.loadSave yet");
        }
        BeginLoad(name, Optional(params, "wait", false), reply);
    }, { .thread = Thread::Main, .timeout = 180s });

    Register("Events.snapshot", [](const Json& params) {
        AllowOnly(params, {});
        return Snapshot();
    }, { .thread = Thread::UI });
}
