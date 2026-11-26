// SPDX-License-Identifier: GPL-3.0-or-later
// Hand-off from the Present hook (perf.cpp) to the sampling profiler (profiler.cpp).
#pragma once

#include <cstdint>

namespace devtools::profiler
{
    // One presented frame on the QPC clock (seconds). Called from inside the Present detour, so it
    // costs one relaxed atomic load unless a profile or the hitch watch is running.
    void OnFrame(double start, double end, std::uint64_t frame);
}
