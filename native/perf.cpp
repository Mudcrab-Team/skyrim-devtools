// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Frame timing from a MinHook detour on the game swap chain's Present, plus the shared hook
// that screen.cpp rides on. The detour only reads a clock and appends to a ring.
#include "events.h"
#include "game.h"
#include "methods.h"
#include "screen.h"

#include <MinHook.h>
#include <Windows.h>

#include <algorithm>
#include <cmath>
#include <mutex>
#include <vector>

namespace
{
    using namespace devtools;

    using PresentFn = HRESULT(__stdcall*)(void*, UINT, UINT);
    constexpr UINT presentTest = 0x1;  // DXGI_PRESENT_TEST: validates only, shows nothing
    constexpr std::size_t presentSlot = 8;  // IDXGISwapChain::Present
    constexpr std::size_t defaultRing = 20000;

    PresentFn originalPresent = nullptr;
    void* mainSwapChain = nullptr;
    bool hooked = false;
    std::atomic<std::uint64_t> frameCounter = 0;

    double QpcSeconds()
    {
        static const double period = [] {
            LARGE_INTEGER f;
            QueryPerformanceFrequency(&f);
            return 1.0 / static_cast<double>(f.QuadPart);
        }();
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        return static_cast<double>(now.QuadPart) * period;
    }

    struct Sample
    {
        double at;  // seconds on the QPC clock when Present was entered
        float ms;   // time since the previous Present
    };

    std::mutex ringMutex;
    std::vector<Sample> ring;
    std::size_t ringHead = 0;   // next slot to write
    std::size_t ringCount = 0;
    double lastPresent = 0.0;
    std::uint64_t hitches = 0;
    std::uint64_t hitchFrame = 0;  // frame number at the most recent hitch
    std::atomic<double> hitchMs = 0.0;

    void Record(double now, std::uint64_t frame)
    {
        float ms = 0.0f;
        bool hitch = false;
        {
            std::lock_guard lock(ringMutex);
            if (lastPresent > 0.0) {
                ms = static_cast<float>((now - lastPresent) * 1000.0);
                if (ring.empty()) {
                    ring.resize(defaultRing);
                }
                ring[ringHead] = { now, ms };
                ringHead = (ringHead + 1) % ring.size();
                ringCount = std::min(ringCount + 1, ring.size());
                const double threshold = hitchMs.load(std::memory_order_relaxed);
                if (threshold > 0.0 && ms > threshold) {
                    ++hitches;
                    hitchFrame = frame;
                    hitch = true;
                }
            }
            lastPresent = now;
        }
        if (hitch && events::Subscribed("frameHitch")) {
            events::Emit("frameHitch", { { "frame", frame }, { "ms", ms }, { "thresholdMs", hitchMs.load() } });
        }
    }

    HRESULT __stdcall HookedPresent(void* swapChain, UINT syncInterval, UINT flags)
    {
        if (swapChain == mainSwapChain && !(flags & presentTest)) {
            const auto frame = frameCounter.fetch_add(1, std::memory_order_relaxed) + 1;
            Record(QpcSeconds(), frame);
            screen::BeforePresent(swapChain, frame);
        }
        return originalPresent(swapChain, syncInterval, flags);
    }

    bool InstallHook()
    {
        const auto window = RE::BSGraphics::Renderer::GetCurrentRenderWindow();
        if (!window || !window->swapChain) {
            return false;
        }
        mainSwapChain = window->swapChain;
        auto** vtable = *reinterpret_cast<void***>(mainSwapChain);
        void* target = vtable[presentSlot];
        return MH_CreateHook(target, reinterpret_cast<void*>(&HookedPresent), reinterpret_cast<void**>(&originalPresent)) == MH_OK &&
               MH_EnableHook(target) == MH_OK;
    }

    double Percentile(const std::vector<float>& sorted, double p)
    {
        const double rank = p * static_cast<double>(sorted.size() - 1);
        const auto low = static_cast<std::size_t>(rank);
        const auto high = std::min(low + 1, sorted.size() - 1);
        return sorted[low] + (sorted[high] - sorted[low]) * (rank - static_cast<double>(low));
    }

    double Round(double value) { return std::round(value * 1000.0) / 1000.0; }
}

namespace devtools::perf
{
    std::uint64_t FrameNumber() { return frameCounter.load(std::memory_order_relaxed); }
    bool PresentHooked() { return hooked; }
}

DEVTOOLS_MODULE(Perf)
{
    events::Declare("frameHitch", "A frame took longer than the configured threshold: {frame, ms, thresholdMs}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            hooked = InstallHook();
            SetCapability("perf", hooked);
            SetCapability("screen", hooked);
        }
    });
    SetCapability("perf", false);
    SetCapability("screen", false);

    Register("Perf.frameStats", [](const Json& params) {
        AllowOnly(params, { "window" });
        if (!hooked) {
            throw RpcError(code::notReady, "The Present hook is not installed");
        }
        const double window = Optional<double>(params, "window", 0.0);
        if (window < 0.0) {
            throw RpcError(code::invalidParams, "window is a number of seconds");
        }
        const double now = QpcSeconds();
        std::vector<float> times;
        std::uint64_t hitchCount = 0;
        std::uint64_t lastHitch = 0;
        {
            std::lock_guard lock(ringMutex);
            times.reserve(ringCount);
            for (std::size_t i = 0; i < ringCount; ++i) {
                const auto& sample = ring[(ringHead + ring.size() - 1 - i) % ring.size()];
                if (window > 0.0 && now - sample.at > window) {
                    break;
                }
                times.push_back(sample.ms);
            }
            hitchCount = hitches;
            lastHitch = hitchFrame;
        }
        Json result{ { "frame", frameCounter.load() }, { "count", times.size() }, { "window", window > 0.0 ? Json(window) : Json(nullptr) },
            { "hitchMs", hitchMs.load() > 0.0 ? Json(hitchMs.load()) : Json(nullptr) }, { "hitches", hitchCount },
            { "lastHitchFrame", lastHitch } };
        if (times.empty()) {
            result.update({ { "meanMs", nullptr }, { "p50Ms", nullptr }, { "p95Ms", nullptr }, { "p99Ms", nullptr },
                { "minMs", nullptr }, { "maxMs", nullptr }, { "fps", nullptr } });
            return result;
        }
        double sum = 0.0;
        for (const auto ms : times) {
            sum += ms;
        }
        std::ranges::sort(times);
        const double mean = sum / static_cast<double>(times.size());
        result.update({ { "meanMs", Round(mean) }, { "p50Ms", Round(Percentile(times, 0.50)) },
            { "p95Ms", Round(Percentile(times, 0.95)) }, { "p99Ms", Round(Percentile(times, 0.99)) },
            { "minMs", Round(times.front()) }, { "maxMs", Round(times.back()) }, { "fps", Round(1000.0 / mean) } });
        return result;
    }, { .thread = Thread::Any });

    Register("Perf.reset", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(ringMutex);
        ringHead = 0;
        ringCount = 0;
        hitches = 0;
        hitchFrame = 0;
        lastPresent = 0.0;
        return Json{ { "reset", true }, { "frame", frameCounter.load() } };
    }, { .thread = Thread::Any });

    Register("Perf.configure", [](const Json& params) {
        AllowOnly(params, { "hitchMs", "ringSize" });
        if (params.contains("hitchMs")) {
            const double value = params["hitchMs"].is_null() ? 0.0 : Require<double>(params, "hitchMs");
            if (value < 0.0 || value > 60000.0) {
                throw RpcError(code::invalidParams, "hitchMs is 0 (off) to 60000");
            }
            hitchMs.store(value);
        }
        if (params.contains("ringSize")) {
            const auto size = Require<std::size_t>(params, "ringSize");
            if (size < 64 || size > 1000000) {
                throw RpcError(code::invalidParams, "ringSize is 64 to 1000000 frames");
            }
            std::lock_guard lock(ringMutex);
            ring.assign(size, Sample{});
            ringHead = 0;
            ringCount = 0;
            lastPresent = 0.0;
        }
        std::lock_guard lock(ringMutex);
        return Json{ { "hitchMs", hitchMs.load() > 0.0 ? Json(hitchMs.load()) : Json(nullptr) },
            { "ringSize", ring.empty() ? defaultRing : ring.size() } };
    }, { .thread = Thread::Any });
}
