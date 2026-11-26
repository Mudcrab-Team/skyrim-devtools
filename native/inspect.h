// SPDX-License-Identifier: GPL-3.0-or-later
// Perception helpers shared by World.inspect and the navigation failure report.
#pragma once

#include "game.h"

#include <string>

namespace devtools::inspect
{
    // The base object's model path ("" when it has none) and its file name without folders.
    [[nodiscard]] std::string ModelPath(RE::TESObjectREFR* ref);
    [[nodiscard]] std::string Basename(const std::string& path);

    // open, opening, closed, closing, or null for references without an open/close state.
    [[nodiscard]] Json OpenState(RE::TESObjectREFR* ref);

    // Why a path that stops at `end` does not reach `goal`: the doors, gates, webs and
    // activators near the stop or between it and the goal, with their state, plus the gap.
    // Call on the main thread.
    [[nodiscard]] Json Blockers(const RE::NiPoint3& end, const RE::NiPoint3& goal);

    // One line for a failure reason, built from Blockers().
    [[nodiscard]] std::string BlockerSummary(const Json& blockers);
}
