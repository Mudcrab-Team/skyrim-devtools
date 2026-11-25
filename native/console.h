// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <RE/Skyrim.h>

#include <string>
#include <string_view>
#include <vector>

namespace devtools::console
{
    // Runs one console command on the calling (main) thread and returns every line the game
    // printed to the console while it ran. Throws RpcError when the console cannot run commands.
    std::vector<std::string> Run(std::string_view command, RE::TESObjectREFR* target);
}
