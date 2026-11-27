// SPDX-License-Identifier: GPL-3.0-or-later
// Walk speed and game-clock control shared by Game.setSpeed and Nav.goto's fast mode.
#pragma once

#include "core.h"

namespace devtools::speed
{
    // Highest walk speed multiplier (percent of normal) the plugin accepts; above it physics and the
    // navmesh follower stop being reliable.
    constexpr float maxWalk = 400.0F;
    constexpr float defaultFast = 300.0F;

    // Raises the player's walk speed to `percent` of normal for an automatic boost; 0 (or at most 100)
    // gives control back to the manual setting.
    void Boost(float percent);

    // Records the steady horizontal speed (units per second) the player was measured to walk at the current
    // walk percent; Game.speed reports it as `measured`.
    void Observe(double unitsPerSecond);
    float AppliedPercent();

    // Moves the game clock forward and reports the before/after reading; throws not-ready without a calendar.
    Json SkipHours(float hours);
}
