// SPDX-License-Identifier: GPL-3.0-or-later
// The references quest markers point at, shared by Quest.targets and Nav.goto's questTarget goal.
#pragma once

#include "game.h"

#include <string>
#include <vector>

namespace devtools::quest
{
    struct Target
    {
        RE::TESQuest* quest = nullptr;
        std::uint16_t objectiveIndex = 0;
        std::string objectiveText;
        std::uint32_t alias = 0;
        RE::TESObjectREFR* target = nullptr;    // what the marker is on
        RE::TESObjectREFR* tracking = nullptr;  // what the compass follows now: the target, or the way to it
    };

    // Targets of the displayed objectives of the quest named by editor id, form id or display name
    // (empty: every running quest), the tracked quest first. Throws notFound for an unknown quest.
    std::vector<Target> CurrentTargets(const std::string& quest, bool pendingOnly);
}
