// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
#include "game.h"

#include <Windows.h>

#include <mutex>
#include <vector>

namespace devtools
{
    namespace
    {
        std::mutex listenersMutex;
        std::vector<std::function<void(SKSE::MessagingInterface::Message&)>>& Listeners()
        {
            static std::vector<std::function<void(SKSE::MessagingInterface::Message&)>> listeners;
            return listeners;
        }
    }

    Config& GetConfig()
    {
        static Config config;
        return config;
    }

    void OnSkseMessage(std::function<void(SKSE::MessagingInterface::Message&)> listener)
    {
        std::lock_guard lock(listenersMutex);
        Listeners().push_back(std::move(listener));
    }

    void DispatchSkseMessage(SKSE::MessagingInterface::Message& message)
    {
        std::vector<std::function<void(SKSE::MessagingInterface::Message&)>> copy;
        {
            std::lock_guard lock(listenersMutex);
            copy = Listeners();
        }
        for (auto& listener : copy) {
            listener(message);
        }
    }

    RE::UI& RequireUI()
    {
        const auto ui = state::dataLoaded ? RE::UI::GetSingleton() : nullptr;
        if (!ui) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        return *ui;
    }

    bool MovieReady(RE::UI& ui, std::string_view name)
    {
        const auto menu = ui.GetMenu(name);
        return menu && menu->uiMovie && menu->uiMovie->IsAvailable("_root");
    }

    bool Playable(RE::UI& ui)
    {
        if (!state::loadCompleted || !state::loadSucceeded || ui.IsMenuOpen("Main Menu") ||
            ui.IsMenuOpen("Loading Menu") || !ui.IsMenuOpen("HUD Menu")) {
            return false;
        }
        const auto player = RE::PlayerCharacter::GetSingleton();
        return player && player->Is3DLoaded();
    }

    void RequirePlayable(const Json& params)
    {
        const auto& ui = RequireUI();
        if (const auto found = params.find("loadEpoch"); found != params.end() && !found->is_null()) {
            if (!found->is_number_unsigned() || found->get<std::uint64_t>() != state::epoch) {
                throw RpcError(code::staleEpoch, "Stale load epoch", { { "loadEpoch", state::epoch.load() } });
            }
        }
        if (!Playable(const_cast<RE::UI&>(ui))) {
            throw RpcError(code::notReady, "No playable world is loaded");
        }
    }

    HWND MainWindow()
    {
        struct Search
        {
            bool visibleOnly;
            HWND found = nullptr;
        };
        for (const bool visibleOnly : { true, false }) {
            Search search{ visibleOnly };
            EnumWindows([](HWND window, LPARAM param) -> BOOL {
                auto& search = *reinterpret_cast<Search*>(param);
                DWORD pid = 0;
                GetWindowThreadProcessId(window, &pid);
                if (pid != GetCurrentProcessId() || GetWindow(window, GW_OWNER) ||
                    (search.visibleOnly && !IsWindowVisible(window))) {
                    return TRUE;
                }
                char className[64]{};
                GetClassNameA(window, className, sizeof(className));
                // A hidden window is only trusted when it is the game's own top-level class.
                if (!search.visibleOnly && std::string_view(className) != "Skyrim Special Edition") {
                    return TRUE;
                }
                search.found = window;
                return FALSE;
            }, reinterpret_cast<LPARAM>(&search));
            if (search.found) {
                return search.found;
            }
        }
        return nullptr;
    }

    bool GameIsForeground()
    {
        DWORD pid = 0;
        GetWindowThreadProcessId(GetForegroundWindow(), &pid);
        return pid == GetCurrentProcessId();
    }
}
