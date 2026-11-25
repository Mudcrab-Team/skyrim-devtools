// SPDX-License-Identifier: GPL-3.0-or-later
// A per-poll hook into the input module for code that drives the player every frame (movement,
// navigation, recording). The listener runs at the start of each input poll on the thread that
// polls input, before any queued requests; whatever it asks to hold is injected exactly like
// Input.control, so the game sees ordinary key presses.
#pragma once

#include <functional>
#include <string>
#include <vector>

namespace devtools::input
{
    struct Drive
    {
        std::vector<std::string> hold;  // ControlMap user events held down for this poll
        std::vector<std::string> tap;   // pressed this poll and released on the next one
    };

    // seconds is the poll's frame delta as the game passed it. Only one listener is kept.
    using PollListener = std::function<void(float seconds, Drive& drive)>;
    void SetPollListener(PollListener listener);
    [[nodiscard]] bool Hooked();
    // Controls actually injected on the latest poll, for recordings.
    [[nodiscard]] std::vector<std::string> DrivenControls();
}
