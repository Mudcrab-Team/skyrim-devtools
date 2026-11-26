// SPDX-License-Identifier: GPL-3.0-or-later
// Robustness options of a playtest session (see docs/extending.md, Options). Include this only from DLL sources.
#pragma once

#include "game.h"

#include <string>

namespace devtools::options
{
    // True when a menu that just opened is on the auto-dismiss list; hides it a moment later and emits
    // menuAutoDismissed. Message boxes are never dismissed.
    void OnMenuOpened(const std::string& menu);

    [[nodiscard]] bool CleanTransition();
    [[nodiscard]] std::string CleanTransitionCell();
    [[nodiscard]] int StallThresholdMs();
}
