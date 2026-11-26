// SPDX-License-Identifier: GPL-3.0-or-later
// Hand-off from the Present hook (perf.cpp) to the sampling profiler (profiler.cpp).
#pragma once

#include "core.h"

#include <cstdint>
#include <string>
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
    // AMD board metrics through the optional ADLX helper, sampled with GpuCounters:
    // {source, reason, gpu, names, supported, sampleUs, rows: [[ms, value per name...]]}; source null without it.
    Json GpuMetrics(double from, double to, double origin);

    // Render-target/depth clear hooks on the immediate context, shared by GPU timing and GPA (profiler_gpu.cpp).
    void EnsurePassHooks(void* immediateContext);
    bool PassHooksOk();
    // "rtv 1920x1080" or "dsv 2048x2048 array" for an ID3D11View.
    std::string ViewLabel(const char* kind, void* view);

    // GPUPerfAPI hardware counters (profiler_gpa.cpp), driven from the Present detour and the clear hooks.
    void GpaBeforePresent(void* swapChain);
    void GpaAfterPresent();
    bool GpaWantsClears();
    void GpaOnClear(void* context, const char* kind, void* view);
}
