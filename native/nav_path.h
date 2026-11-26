// SPDX-License-Identifier: GPL-3.0-or-later
// Paths over the navmeshes of the currently loaded cells.
#pragma once

#include "nav_common.h"
#include "nav_global.h"

#include <cstdint>
#include <vector>

namespace devtools::nav
{
    struct Plan
    {
        std::vector<RE::NiPoint3> points;  // start, bends ..., end
        bool found = false;                // the start lies on a loaded navmesh
        bool complete = false;             // reaches the goal; otherwise ends nearest to it
        bool goalOnMesh = false;           // the goal lies on a loaded navmesh
        std::size_t expanded = 0;
        std::size_t triangles = 0;
        std::size_t meshes = 0;
        double ms = 0.0;
        std::string note;
    };

    Plan PlanPath(const RE::NiPoint3& from, const RE::NiPoint3& to);

    // Changes whenever the set of loaded navmeshes changes, so callers know to plan again.
    std::uint64_t GraphGeneration();

    Json PlanJson(const Plan& plan);

    // Walking cost over the loaded navmeshes from `from` to each target (infinity when a target
    // is off the loaded navmeshes, unreachable or dearer than maxCost); empty when `from` is off them.
    std::vector<double> CostsTo(const RE::NiPoint3& from, const std::vector<RE::NiPoint3>& targets, double maxCost);

    // A long-range route over the worldspace's offline graph (see nav_global.h).
    struct GlobalPlan
    {
        global::Route route;
        bool available = false;  // a graph file exists for the worldspace
        std::string world;
        std::string file;
        std::string note;
        double ms = 0.0;
    };

    // Whether a graph file exists for the worldspace (loaded on first use).
    bool HasGlobalGraph(RE::TESWorldSpace* world);
    GlobalPlan PlanGlobal(RE::TESWorldSpace* world, const RE::NiPoint3& from, const RE::NiPoint3& to);
    Json GlobalJson(const GlobalPlan& plan, bool points = true);
}
