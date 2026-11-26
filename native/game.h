// SPDX-License-Identifier: GPL-3.0-or-later
// Game-side state and helpers shared by modules. Include this only from DLL sources.
#pragma once

#include "core.h"

#include <RE/Skyrim.h>
#include <SKSE/SKSE.h>

#include <atomic>
#include <filesystem>
#include <functional>
#include <string>

namespace devtools
{
    struct Config
    {
        std::string sessionId;
        std::string build;
        std::string imageSha256;
        bool headless = false;
        std::filesystem::path savesDir;  // empty when the host did not say where saves live
    };

    Config& GetConfig();

    namespace state
    {
        inline std::atomic_bool dataLoaded = false;
        inline std::atomic_bool loadCompleted = false;
        inline std::atomic_bool loadSucceeded = false;
        // Increments on every requested load; replies and events carry it so a client can tell
        // which world a result belongs to.
        inline std::atomic<std::uint64_t> epoch = 0;
    }

    // SKSE messages fan out to every module that asked for them.
    void OnSkseMessage(std::function<void(SKSE::MessagingInterface::Message&)> listener);
    void DispatchSkseMessage(SKSE::MessagingInterface::Message& message);

    RE::UI& RequireUI();
    bool MovieReady(RE::UI& ui, std::string_view menu);
    bool Playable(RE::UI& ui);
    // Throws not-ready unless the world is playable and, when the caller passed loadEpoch, unchanged.
    void RequirePlayable(const Json& params);

    // Presses a ControlMap user event the way a player's key does (needs the input hook). False when
    // input injection is unavailable.
    bool TapControl(const std::string& control);

    HWND MainWindow();
    bool GameIsForeground();
}
