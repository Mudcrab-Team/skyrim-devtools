// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Frame timing from a MinHook detour on the game swap chain's Present, plus the shared hook
// that screen.cpp rides on. The detour only reads a clock and appends to a ring.
#include "events.h"
#include "game.h"
#include "logs.h"
#include "methods.h"
#include "perf.h"
#include "profiler.h"
#include "screen.h"
#include "trace.h"

#include <MinHook.h>
#include <Windows.h>
#include <psapi.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <map>
#include <mutex>
#include <thread>
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

    // One slow frame with what the frame thread itself was doing: cpuMs near ms means it was computing,
    // far below ms means it was blocked (GPU, disk, a lock); kernelMs says how much of the work was system calls.
    struct Hitch
    {
        double at;
        std::uint64_t frame;
        float ms;
        float cpuMs;
        float kernelMs;
        float presentMs;  // time the previous frame spent inside the real Present
    };

    struct Call
    {
        std::string method;
        const char* thread;
        double start;
        float queuedMs;
        float ranMs;
    };

    // Process counters every 250 ms, so a hitch can be set against paging, disk traffic and CPU.
    struct Counters
    {
        double at;
        std::uint64_t faults;
        std::uint64_t workingSet;
        std::uint64_t readBytes;
        std::uint64_t writeBytes;
        std::uint64_t cpu100ns;
        std::uint64_t availPhys;
    };

    constexpr std::size_t maxLog = 4000;
    constexpr std::size_t maxCounters = 4800;
    std::mutex logMutex;
    std::deque<Hitch> hitchLog;
    std::deque<Call> callLog;
    std::deque<Counters> counterLog;
    std::atomic<double> hitchLogMs = 100.0;
    double epochOffset = 0.0;  // system clock seconds minus QPC seconds
    std::atomic<float> lastPresentMs = 0.0f;
    std::uint64_t prevThreadCpu = 0;
    std::uint64_t prevThreadKernel = 0;

    std::uint64_t FileTime(const FILETIME& t)
    {
        return (static_cast<std::uint64_t>(t.dwHighDateTime) << 32) | t.dwLowDateTime;
    }

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
        FILETIME created, exited, kernel, user;
        float cpuMs = 0.0f;
        float kernelMs = 0.0f;
        double frameStart = 0.0;
        if (GetThreadTimes(GetCurrentThread(), &created, &exited, &kernel, &user)) {
            const auto k = FileTime(kernel);
            const auto total = k + FileTime(user);
            cpuMs = static_cast<float>(static_cast<double>(total - prevThreadCpu) / 10000.0);
            kernelMs = static_cast<float>(static_cast<double>(k - prevThreadKernel) / 10000.0);
            prevThreadCpu = total;
            prevThreadKernel = k;
        }
        {
            std::lock_guard lock(ringMutex);
            frameStart = lastPresent;
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
        if (frameStart > 0.0) {
            profiler::OnFrame(frameStart, now, frame);
        }
        if (ms > hitchLogMs.load(std::memory_order_relaxed) && ms > 0.0f) {
            std::lock_guard lock(logMutex);
            hitchLog.push_back({ now, frame, ms, cpuMs, kernelMs, lastPresentMs.load(std::memory_order_relaxed) });
            if (hitchLog.size() > maxLog) {
                hitchLog.pop_front();
            }
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
            profiler::GpuBeforePresent(swapChain);
            trace::BeforePresent(swapChain);
            const double before = QpcSeconds();
            const auto result = originalPresent(swapChain, syncInterval, flags);
            lastPresentMs.store(static_cast<float>((QpcSeconds() - before) * 1000.0), std::memory_order_relaxed);
            trace::AfterPresent(frame + 1);
            profiler::GpuAfterPresent(frame + 1);
            return result;
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

    void SampleCounters()
    {
        const HANDLE self = GetCurrentProcess();
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            PROCESS_MEMORY_COUNTERS memory{ sizeof(memory) };
            IO_COUNTERS io{};
            FILETIME created, exited, kernel, user;
            MEMORYSTATUSEX system{ sizeof(system) };
            if (!GetProcessMemoryInfo(self, &memory, sizeof(memory)) || !GetProcessIoCounters(self, &io) ||
                !GetProcessTimes(self, &created, &exited, &kernel, &user) || !GlobalMemoryStatusEx(&system)) {
                continue;
            }
            std::lock_guard lock(logMutex);
            counterLog.push_back({ QpcSeconds(), memory.PageFaultCount, memory.WorkingSetSize, io.ReadTransferCount,
                io.WriteTransferCount, FileTime(kernel) + FileTime(user), system.ullAvailPhys });
            if (counterLog.size() > maxCounters) {
                counterLog.pop_front();
            }
        }
    }

    double Round(double value) { return std::round(value * 1000.0) / 1000.0; }
}

namespace devtools::perf
{
    std::uint64_t FrameNumber() { return frameCounter.load(std::memory_order_relaxed); }
    bool PresentHooked() { return hooked; }
    double Now() { return QpcSeconds(); }

    RecentFrames Recent(double seconds)
    {
        RecentFrames out;
        out.hooked = hooked;
        if (!hooked) {
            return out;
        }
        const double now = QpcSeconds();
        std::lock_guard lock(ringMutex);
        for (std::size_t i = 0; i < ringCount; ++i) {
            const auto& sample = ring[(ringHead + ring.size() - 1 - i) % ring.size()];
            if (now - sample.at > seconds) {
                break;
            }
            ++out.count;
            out.maxMs = std::max(out.maxMs, static_cast<double>(sample.ms));
        }
        // Time since the latest present counts as one long frame, so a stalled game shows up at once.
        if (lastPresent > 0.0) {
            out.maxMs = std::max(out.maxMs, (now - lastPresent) * 1000.0);
        }
        return out;
    }

    Json CounterSeries(double from, double to, double origin)
    {
        auto out = Json::array();
        std::lock_guard lock(logMutex);
        for (const auto& c : counterLog) {
            if (c.at >= from && c.at <= to) {
                out.push_back({ std::round((c.at - origin) * 1e5) / 100.0, c.faults, c.workingSet, c.readBytes, c.writeBytes,
                    c.cpu100ns, c.availPhys });
            }
        }
        return out;
    }

    void NoteCall(const std::string& method, const char* thread, double startedAt, double queuedMs, double ranMs)
    {
        profiler::Mark("rpc", method, startedAt, startedAt + ranMs / 1000.0);
        if (ranMs < 5.0 && queuedMs < 100.0) {
            return;
        }
        {
            std::lock_guard lock(logMutex);
            callLog.push_back({ method, thread, startedAt, static_cast<float>(queuedMs), static_cast<float>(ranMs) });
            if (callLog.size() > maxLog) {
                callLog.pop_front();
            }
        }
        if (ranMs >= 20.0) {
            logs::Plugin("warning", method + " held the " + thread + " thread for " + std::to_string(static_cast<int>(ranMs)) + " ms");
        }
    }
}

DEVTOOLS_MODULE(Perf)
{
    events::Declare("frameHitch", "A frame took longer than the configured threshold: {frame, ms, thresholdMs}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            epochOffset = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count() - QpcSeconds();
            std::thread(SampleCounters).detach();
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

    Register("Perf.hitches", [](const Json& params) {
        AllowOnly(params, { "limit", "sinceFrame", "clear", "calls" });
        const auto limit = Optional<std::size_t>(params, "limit", 50);
        const auto since = Optional<std::uint64_t>(params, "sinceFrame", 0);
        const bool callsToo = Optional<bool>(params, "calls", true);
        const double now = QpcSeconds();
        std::vector<Hitch> hitchesOut;
        std::vector<Call> callsOut;
        std::vector<Counters> counters;
        std::size_t total = 0;
        {
            std::lock_guard lock(logMutex);
            total = hitchLog.size();
            for (auto it = hitchLog.rbegin(); it != hitchLog.rend() && hitchesOut.size() < limit; ++it) {
                if (it->frame > since) {
                    hitchesOut.push_back(*it);
                }
            }
            callsOut.assign(callLog.begin(), callLog.end());
            counters.assign(counterLog.begin(), counterLog.end());
            if (Optional<bool>(params, "clear", false)) {
                hitchLog.clear();
                callLog.clear();
            }
        }
        std::ranges::reverse(hitchesOut);
        const auto counterAt = [&](double at, bool after) -> const Counters* {
            const Counters* found = nullptr;
            for (const auto& c : counters) {
                if (!after && c.at <= at) {
                    found = &c;
                } else if (after && c.at >= at) {
                    return &c;
                }
            }
            return after ? nullptr : found;
        };
        auto list = Json::array();
        for (const auto& h : hitchesOut) {
            const double begin = h.at - h.ms / 1000.0;
            Json entry{ { "frame", h.frame }, { "epochMs", static_cast<std::int64_t>((h.at + epochOffset) * 1000.0) },
                { "agoSec", Round(now - h.at) }, { "ms", Round(h.ms) }, { "threadCpuMs", Round(h.cpuMs) },
                { "threadKernelMs", Round(h.kernelMs) }, { "blockedMs", Round(std::max(0.0f, h.ms - h.cpuMs)) },
                { "prevPresentMs", Round(h.presentMs) } };
            if (const auto* a = counterAt(begin, false)) {
                if (const auto* b = counterAt(h.at, true)) {
                    entry.update({ { "pageFaults", b->faults - a->faults },
                        { "readMB", Round(static_cast<double>(b->readBytes - a->readBytes) / 1048576.0) },
                        { "writeMB", Round(static_cast<double>(b->writeBytes - a->writeBytes) / 1048576.0) },
                        { "processCpuMs", Round(static_cast<double>(b->cpu100ns - a->cpu100ns) / 10000.0) },
                        { "workingSetMB", Round(static_cast<double>(b->workingSet) / 1048576.0) },
                        { "availPhysMB", Round(static_cast<double>(b->availPhys) / 1048576.0) } });
                }
            }
            auto overlap = Json::array();
            for (const auto& c : callsOut) {
                const double end = c.start + c.ranMs / 1000.0;
                if (end >= begin && c.start <= h.at && c.ranMs >= 5.0f) {
                    overlap.push_back({ { "method", c.method }, { "thread", c.thread }, { "ranMs", Round(c.ranMs) } });
                }
            }
            entry["overlappingCalls"] = std::move(overlap);
            list.push_back(std::move(entry));
        }
        struct Tally { std::size_t count = 0; double total = 0.0; double max = 0.0; };
        std::map<std::string, Tally> tallies;
        for (const auto& c : callsOut) {
            auto& t = tallies[c.method];
            ++t.count;
            t.total += c.ranMs;
            t.max = std::max(t.max, static_cast<double>(c.ranMs));
        }
        Json result{ { "thresholdMs", hitchLogMs.load() }, { "logged", total }, { "hitches", std::move(list) } };
        if (callsToo) {
            auto slow = Json::object();
            for (const auto& [method, t] : tallies) {
                slow[method] = { { "count", t.count }, { "totalMs", Round(t.total) }, { "maxMs", Round(t.max) } };
            }
            result["slowCalls"] = std::move(slow);
        }
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
        {
            std::lock_guard logLock(logMutex);
            hitchLog.clear();
            callLog.clear();
        }
        return Json{ { "reset", true }, { "frame", frameCounter.load() } };
    }, { .thread = Thread::Any });

    Register("Perf.configure", [](const Json& params) {
        AllowOnly(params, { "hitchMs", "ringSize", "hitchLogMs" });
        if (params.contains("hitchLogMs")) {
            hitchLogMs.store(std::clamp(Require<double>(params, "hitchLogMs"), 1.0, 60000.0));
        }
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
