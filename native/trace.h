// SPDX-License-Identifier: GPL-3.0-or-later
// Event tracing alongside the sampling profiler: engine function ranges, D3D11 calls, OS waits and file
// reads, recorded into per-thread buffers on the QPC clock while a profile with `trace` runs. The client
// turns them into a Perfetto trace (skyrim_devtools/profile_trace.py).
#pragma once

#include "core.h"

#include <cstdint>
#include <string>
#include <string_view>

namespace devtools::trace
{
    // One relaxed load; every hook checks it first so an idle trace costs a branch.
    bool Active();
    std::int64_t Ticks();

    // Profile.start's `trace` object; installs whatever hooks it names on first use. Returns what was
    // installed (hook results are also part of the serialized trace).
    Json Start(const Json& config);
    void Stop();
    // Every event since Start, with ticks relative to `origin` (QPC seconds) recoverable by the client.
    Json Serialize(double origin);

    // Interned event name; category groups names in the export (engine, d3d11, pass, wait, io, frame).
    std::uint32_t Name(std::string_view name, std::string_view category);
    // A finished range on the calling thread. `flags` (12 bits) rides in the name word.
    void Complete(std::uint32_t name, std::int64_t start, std::int64_t end, std::uint64_t a = 0, std::uint64_t b = 0,
        std::uint32_t flags = 0);

    // Render-thread hand-offs from the Present detour (perf.cpp) and the clear hooks (profiler_gpu.cpp).
    void BeforePresent(void* swapChain);
    void AfterPresent(std::uint64_t nextFrame);
    void OnPassBoundary(void* context, const char* kind, void* view);

    // Hook sets, each in its own file.
    Json InstallEngineHooks(const Json& hooks);
    Json EngineHookStatus();
    Json ApiStatus();
    void ResetApiSession();
    Json InstallOsHooks();
    Json OsStatus();
    void InstallFileMapHooks();
    // Short waits are counted per thread instead of recorded.
    void CountShortWait();

    struct Settings
    {
        int apiMode = 1;               // 0 off, 1 per-pass batches, 2 every call
        bool os = true;
        std::int64_t waitMinTicks = 0;  // shorter waits are only counted
        std::uint64_t maxEvents = 2'000'000;
    };
    const Settings& Current();
}
