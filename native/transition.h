// SPDX-License-Identifier: GPL-3.0-or-later
// Clean cell transitions: a jump to another cell is preceded by a visit to a small cell, so the game
// shows a real loading screen and unloads the old surroundings first. Include this only from DLL sources.
#pragma once

#include "game.h"

#include <functional>
#include <string_view>

namespace devtools::transition
{
    // True when the command is a cell or worldspace jump (coc, cow and their long forms), the option is on and
    // the player is not already in the bounce cell.
    [[nodiscard]] bool Wanted(std::string_view command);

    // Sends the player to the bounce cell, waits until its loading screen is over and the world is playable
    // again, then runs `proceed` on the main thread. Fails the reply with a timeout when the bounce never
    // completes. Call from the main thread.
    void Bounce(const ReplyPtr& reply, std::function<void()> proceed);
}
