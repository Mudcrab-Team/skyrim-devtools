// SPDX-License-Identifier: GPL-3.0-or-later
// Hand-off from the Present hook (perf.cpp) to the sampling profiler (profiler.cpp).
#pragma once

#include "core.h"

#include <cstdint>
#include <string_view>

namespace devtools::profiler
{
    // One presented frame on the QPC clock (seconds). Called from inside the Present detour, so it
    // costs one relaxed atomic load unless a profile or the hitch watch is running.
    void OnFrame(double start, double end, std::uint64_t frame);

    bool Profiling();

    // A named span on the QPC clock for the timeline view (RPC handlers, loading screens, client markers).
    // Kept only while a profile runs.
    void Mark(std::string_view kind, std::string_view name, double start, double end);

    // Self/total/wait counts of the main thread over the last `seconds`, resolved like a profile's frames.
    // Each call keeps the main-thread sampler running for a further 10 s.
    Json TopSamples(double seconds);
}
