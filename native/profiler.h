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

    // True while a profile runs or the live view's lease holds the sampler on.
    bool Sampling();

    // GPU timing (profiler_gpu.cpp), called from the Present detour on the render thread.
    void GpuBeforePresent(void* swapChain);
    void GpuAfterPresent(std::uint64_t nextFrame);
    // Timed frames whose CPU span meets [from, to] (QPC seconds), CPU times in ms after `origin`.
    Json GpuFrames(double from, double to, double origin);
    // GPU frame-time percentiles and the costliest passes over the last `seconds`.
    Json GpuRecent(double seconds);
    // GPU utilisation and VRAM sampled once a second while a profile runs (profiler_top.cpp).
    Json GpuCounters(double from, double to, double origin);
}
