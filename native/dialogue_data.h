// SPDX-License-Identifier: GPL-3.0-or-later
// Spoken-line notifications the coverage report listens to.
#pragma once

#include "game.h"

#include <functional>

namespace devtools::dialogue
{
    struct InfoSaid
    {
        RE::FormID info = 0;
        RE::FormID topic = 0;
        RE::FormID quest = 0;
        std::int64_t ms = 0;
    };

    // Called from the watcher thread each time the NPC starts a different dialogue info.
    void OnInfoSaid(std::function<void(const InfoSaid&)> listener);
}
