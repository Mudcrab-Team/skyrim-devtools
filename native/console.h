// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <RE/Skyrim.h>

#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace devtools::console
{
    // Runs one console command on the calling (main) thread and returns every line the game
    // printed to the console while it ran. Throws RpcError when the console cannot run commands.
    std::vector<std::string> Run(std::string_view command, RE::TESObjectREFR* target);

    // Told of every command the game compiles from the console: those typed by a person (typed = true,
    // seen through a detour of the console's compiler entry) and those Run executes (typed = false).
    // Runs on the game thread. Only one observer is kept.
    using CommandObserver = std::function<void(std::string_view command, bool typed)>;
    void SetCommandObserver(CommandObserver observer);
}
