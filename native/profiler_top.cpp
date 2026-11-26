// SPDX-License-Identifier: GPL-3.0-or-later
// Profile.top: one snapshot for a live dashboard. Main-thread hot functions come from the profiler's ring;
// process memory and per-thread CPU are read here. Rates (page faults/s, CPU %) are deltas against the previous
// call, so the first call after a pause reports them as null. GPU counters come from a background collector,
// because one WDDM GPU Engine collection walks every engine instance on the system and can take longer than a
// dashboard refresh.
#include "adlx/devtools_adlx.h"

#include "methods.h"
#include "perf.h"
#include "profiler.h"
#include "thread_names.h"

#include <Windows.h>
#include <TlHelp32.h>
#include <pdh.h>
#include <pdhmsg.h>
#include <psapi.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#pragma comment(lib, "pdh.lib")

using namespace devtools;

namespace
{
    std::mutex topStateMutex;

    double Round(double value, double scale = 10.0)
    {
        return std::round(value * scale) / scale;
    }

    std::string Narrow(const wchar_t* text)
    {
        if (!text || !*text) {
            return {};
        }
        const int size = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
        std::string out(static_cast<std::size_t>(std::max(size, 1) - 1), '\0');
        WideCharToMultiByte(CP_UTF8, 0, text, -1, out.data(), size, nullptr, nullptr);
        return out;
    }

    double FileTimeSeconds(const FILETIME& time)
    {
        return static_cast<double>((static_cast<std::uint64_t>(time.dwHighDateTime) << 32) | time.dwLowDateTime) / 1e7;
    }

    struct MemoryState
    {
        double at = 0.0;
        DWORD faults = 0;
    } memoryState;

    Json Memory(double now)
    {
        PROCESS_MEMORY_COUNTERS_EX counters{};
        counters.cb = sizeof(counters);
        if (!GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&counters), sizeof(counters))) {
            return nullptr;
        }
        constexpr double mb = 1024.0 * 1024.0;
        Json out{ { "workingSetMb", Round(counters.WorkingSetSize / mb) }, { "peakWorkingSetMb", Round(counters.PeakWorkingSetSize / mb) },
            { "privateMb", Round(counters.PrivateUsage / mb) }, { "pagefileMb", Round(counters.PagefileUsage / mb) },
            { "pageFaults", counters.PageFaultCount } };
        // Working set well below private bytes means pages were trimmed and will fault back in.
        out["residentPct"] = counters.PrivateUsage ? Round(100.0 * counters.WorkingSetSize / counters.PrivateUsage) : 0.0;
        if (memoryState.at > 0.0 && now - memoryState.at < 30.0) {
            out["pageFaultsPerS"] = Round((counters.PageFaultCount - memoryState.faults) / (now - memoryState.at));
        } else {
            out["pageFaultsPerS"] = nullptr;
        }
        memoryState = { now, counters.PageFaultCount };
        MEMORYSTATUSEX system{};
        system.dwLength = sizeof(system);
        if (GlobalMemoryStatusEx(&system)) {
            out["systemAvailableMb"] = Round(system.ullAvailPhys / mb);
            out["systemTotalMb"] = Round(system.ullTotalPhys / mb);
        }
        return out;
    }

    struct ThreadState
    {
        double at = 0.0;
        std::unordered_map<DWORD, double> cpu;
        double processCpu = 0.0;
    } threadState;

    Json Threads(double now, std::size_t limit)
    {
        const DWORD pid = GetCurrentProcessId();
        const HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if (snapshot == INVALID_HANDLE_VALUE) {
            return nullptr;
        }
        struct Row
        {
            DWORD tid;
            std::string name;
            double seconds;
            double pct;
        };
        std::vector<Row> rows;
        THREADENTRY32 entry{ sizeof(entry) };
        for (BOOL ok = Thread32First(snapshot, &entry); ok; ok = Thread32Next(snapshot, &entry)) {
            if (entry.th32OwnerProcessID != pid) {
                continue;
            }
            const HANDLE thread = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ThreadID);
            if (!thread) {
                continue;
            }
            FILETIME created, exited, kernel, user;
            if (GetThreadTimes(thread, &created, &exited, &kernel, &user)) {
                Row row{ entry.th32ThreadID, {}, FileTimeSeconds(kernel) + FileTimeSeconds(user), -1.0 };
                row.name = devtools::threads::NameOf(entry.th32ThreadID, thread).text;
                rows.push_back(std::move(row));
            }
            CloseHandle(thread);
        }
        CloseHandle(snapshot);

        const bool haveRate = threadState.at > 0.0 && now - threadState.at < 30.0;
        const double wall = now - threadState.at;
        std::unordered_map<DWORD, double> cpu;
        for (auto& row : rows) {
            cpu[row.tid] = row.seconds;
            if (haveRate) {
                const auto previous = threadState.cpu.find(row.tid);
                row.pct = (row.seconds - (previous == threadState.cpu.end() ? 0.0 : previous->second)) / wall * 100.0;
            }
        }
        FILETIME created, exited, kernel, user;
        double processCpu = 0.0;
        if (GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user)) {
            processCpu = FileTimeSeconds(kernel) + FileTimeSeconds(user);
        }
        Json out{ { "count", rows.size() } };
        out["processCpuPct"] = haveRate ? Json(Round((processCpu - threadState.processCpu) / wall * 100.0)) : Json(nullptr);
        SYSTEM_INFO info{};
        GetSystemInfo(&info);
        out["cores"] = info.dwNumberOfProcessors;
        threadState = { now, std::move(cpu), processCpu };

        std::ranges::sort(rows, [](const Row& a, const Row& b) { return a.pct != b.pct ? a.pct > b.pct : a.seconds > b.seconds; });
        auto list = Json::array();
        for (std::size_t i = 0; i < rows.size() && i < limit; ++i) {
            list.push_back({ { "tid", rows[i].tid }, { "name", rows[i].name }, { "cpuS", Round(rows[i].seconds, 100.0) },
                { "pct", rows[i].pct < 0.0 ? Json(nullptr) : Json(Round(rows[i].pct)) } });
        }
        out["top"] = std::move(list);
        return out;
    }

    // GPU engine and process-memory counters from the WDDM performance counters. Utilisation counters need
    // two collections, so the first call returns no utilisation.
    struct Gpu
    {
        PDH_HQUERY query = nullptr;
        PDH_HCOUNTER engine = nullptr;
        PDH_HCOUNTER memory = nullptr;
        bool failed = false;
        bool primed = false;

        bool Open()
        {
            if (query || failed) {
                return query != nullptr;
            }
            if (PdhOpenQueryW(nullptr, 0, &query) != ERROR_SUCCESS) {
                failed = true;
                query = nullptr;
                return false;
            }
            const bool engineOk = PdhAddEnglishCounterW(query, L"\\GPU Engine(*)\\Utilization Percentage", 0, &engine) == ERROR_SUCCESS;
            const bool memoryOk = PdhAddEnglishCounterW(query, L"\\GPU Process Memory(*)\\Dedicated Usage", 0, &memory) == ERROR_SUCCESS;
            if (!engineOk && !memoryOk) {
                PdhCloseQuery(query);
                query = nullptr;
                failed = true;
                return false;
            }
            if (!engineOk) {
                engine = nullptr;
            }
            if (!memoryOk) {
                memory = nullptr;
            }
            return true;
        }

        static std::vector<std::pair<std::wstring, double>> Read(PDH_HCOUNTER counter, DWORD format)
        {
            std::vector<std::pair<std::wstring, double>> out;
            if (!counter) {
                return out;
            }
            DWORD bytes = 0;
            DWORD count = 0;
            if (PdhGetFormattedCounterArrayW(counter, format, &bytes, &count, nullptr) != PDH_MORE_DATA || !bytes) {
                return out;
            }
            std::vector<std::byte> buffer(bytes);
            auto* items = reinterpret_cast<PDH_FMT_COUNTERVALUE_ITEM_W*>(buffer.data());
            if (PdhGetFormattedCounterArrayW(counter, format, &bytes, &count, items) != ERROR_SUCCESS) {
                return out;
            }
            for (DWORD i = 0; i < count; ++i) {
                if (items[i].FmtValue.CStatus != PDH_CSTATUS_VALID_DATA && items[i].FmtValue.CStatus != PDH_CSTATUS_NEW_DATA) {
                    continue;
                }
                const double value = format == PDH_FMT_LARGE ? static_cast<double>(items[i].FmtValue.largeValue) : items[i].FmtValue.doubleValue;
                out.emplace_back(items[i].szName ? items[i].szName : L"", value);
            }
            return out;
        }

        Json Sample()
        {
            if (!Open() || PdhCollectQueryData(query) != ERROR_SUCCESS) {
                return nullptr;
            }
            const std::wstring prefix = L"pid_" + std::to_wstring(GetCurrentProcessId()) + L"_";
            Json out = Json::object();
            if (primed) {
                std::map<std::wstring, double> byType;
                for (const auto& [name, value] : Read(engine, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100)) {
                    if (!name.starts_with(prefix)) {
                        continue;
                    }
                    const auto type = name.find(L"engtype_");
                    byType[type == std::wstring::npos ? L"?" : name.substr(type + 8)] += value;
                }
                double busiest = 0.0;
                auto engines = Json::object();
                for (const auto& [type, value] : byType) {
                    busiest = std::max(busiest, value);
                    if (value >= 0.5) {
                        engines[Narrow(type.c_str())] = Round(value);
                    }
                }
                out["utilPct"] = byType.empty() ? Json(nullptr) : Json(Round(std::min(busiest, 100.0)));
                out["engines"] = std::move(engines);
            } else {
                out["utilPct"] = nullptr;
            }
            double dedicated = 0.0;
            bool any = false;
            for (const auto& [name, value] : Read(memory, PDH_FMT_LARGE)) {
                if (name.starts_with(prefix)) {
                    dedicated += value;
                    any = true;
                }
            }
            out["vramMb"] = any ? Json(Round(dedicated / (1024.0 * 1024.0))) : Json(nullptr);
            primed = true;
            return out;
        }
    } gpu;

    // Collects GPU counters once a second while Profile.top was called in the last 10 s, then exits; the call
    // returns the latest result and its age, so its latency no longer depends on PDH.
    struct GpuCollector
    {
        std::mutex mutex;
        Json latest = nullptr;
        double latestAt = 0.0;
        double lastCostMs = 0.0;
        std::atomic<double> leaseUntil = 0.0;
        std::atomic<bool> running = false;

        Json Get(double now)
        {
            leaseUntil = now + 10.0;
            if (!running.exchange(true)) {
                std::thread([this] { Run(); }).detach();
            }
            std::lock_guard lock(mutex);
            if (latest.is_null()) {
                return nullptr;
            }
            Json out = latest;
            out["ageMs"] = Round((now - latestAt) * 1000.0);
            out["collectMs"] = Round(lastCostMs);
            return out;
        }

        void Run()
        {
            while (perf::Now() < leaseUntil.load()) {
                const double started = perf::Now();
                Json sample = gpu.Sample();
                const double finished = perf::Now();
                {
                    std::lock_guard lock(mutex);
                    if (!sample.is_null()) {
                        latest = std::move(sample);
                        latestAt = finished;
                    }
                    lastCostMs = (finished - started) * 1000.0;
                }
                const double rest = 1.0 - (finished - started);
                if (rest > 0.0) {
                    std::this_thread::sleep_for(std::chrono::duration<double>(rest));
                }
            }
            running = false;
            // A call that raced the exit renews the lease without starting a thread; pick it up.
            if (perf::Now() < leaseUntil.load() && !running.exchange(true)) {
                Run();
            }
        }
    } gpuCollector;

    using AdlxValues = std::array<double, DEVTOOLS_ADLX_METRICS>;

    // AMD board metrics (utilisation, clocks, power, temperatures, fan, VRAM) through the optional
    // SkyrimDevToolsAdlx.dll helper next to the plugin. Without the helper or an AMD driver this stays a no-op.
    struct Adlx
    {
        std::mutex mutex;
        bool tried = false;
        bool open = false;
        std::string reason = "not opened yet";
        std::string gpuName;
        unsigned supported = 0;
        DevtoolsAdlxSampleFn sample = nullptr;
        double sampleSeconds = 0.0;
        std::uint64_t samples = 0;

        static std::wstring PluginDirectory()
        {
            HMODULE self = nullptr;
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                    reinterpret_cast<LPCWSTR>(&PluginDirectory), &self)) {
                return {};
            }
            std::wstring path(MAX_PATH, L'\0');
            const DWORD size = GetModuleFileNameW(self, path.data(), static_cast<DWORD>(path.size()));
            path.resize(size);
            const auto slash = path.find_last_of(L"\\/");
            return slash == std::wstring::npos ? std::wstring() : path.substr(0, slash + 1);
        }

        // Called with the mutex held.
        bool Open()
        {
            if (tried) {
                return open;
            }
            tried = true;
            const HMODULE helper = LoadLibraryExW((PluginDirectory() + DEVTOOLS_ADLX_DLL).c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
            if (!helper) {
                reason = "SkyrimDevToolsAdlx.dll is not installed next to the plugin";
                return false;
            }
            const auto version = reinterpret_cast<DevtoolsAdlxVersionFn>(GetProcAddress(helper, "DevtoolsAdlxVersion"));
            const auto openFn = reinterpret_cast<DevtoolsAdlxOpenFn>(GetProcAddress(helper, "DevtoolsAdlxOpen"));
            sample = reinterpret_cast<DevtoolsAdlxSampleFn>(GetProcAddress(helper, "DevtoolsAdlxSample"));
            if (!version || !openFn || !sample || version() != DEVTOOLS_ADLX_VERSION) {
                reason = "SkyrimDevToolsAdlx.dll does not match this plugin";
                return false;
            }
            char name[128]{};
            const int status = openFn(name, sizeof(name), &supported);
            if (status < 0) {
                reason = status == -1 ? "no AMD driver (amdadlx64.dll not found)" : "ADLX refused (" + std::to_string(status) + ")";
                return false;
            }
            gpuName = name;
            reason.clear();
            open = true;
            return true;
        }

        bool Read(AdlxValues& values)
        {
            std::lock_guard lock(mutex);
            if (!Open()) {
                return false;
            }
            const double started = perf::Now();
            const bool ok = sample(values.data(), static_cast<int>(values.size())) == 0;
            sampleSeconds += perf::Now() - started;
            ++samples;
            return ok;
        }

        Json Describe()
        {
            std::lock_guard lock(mutex);
            Json out{ { "source", open ? Json("adlx") : Json(nullptr) }, { "reason", reason } };
            if (open) {
                static constexpr const char* names[] = DEVTOOLS_ADLX_NAMES;
                auto supportedNames = Json::array();
                for (int i = 0; i < DEVTOOLS_ADLX_METRICS; ++i) {
                    if (supported >> i & 1u) {
                        supportedNames.push_back(names[i]);
                    }
                }
                out["gpu"] = gpuName;
                out["names"] = Json(std::vector<std::string>(std::begin(names), std::end(names)));
                out["supported"] = std::move(supportedNames);
                out["sampleUs"] = samples ? Round(sampleSeconds / static_cast<double>(samples) * 1e6) : 0.0;
            }
            return out;
        }
    } adlx;

    Json AdlxRow(const AdlxValues& values)
    {
        auto row = Json::array();
        for (const double value : values) {
            row.push_back(std::isnan(value) ? Json(nullptr) : Json(Round(value, 100.0)));
        }
        return row;
    }

    // A second PDH query for profiles, so the live view's deltas and the profile's series do not disturb
    // each other.
    struct GpuSample
    {
        double at;
        Json utilPct;
        Json vramMb;
        bool hasAdlx;
        AdlxValues adlx;
    };

    std::mutex sessionGpuMutex;
    std::deque<GpuSample> sessionGpu;

    void SampleSessionGpu()
    {
        Gpu counters;
        for (;;) {
            Sleep(1000);
            if (!profiler::Profiling()) {
                counters.primed = false;  // a rate across an idle gap would average over it
                continue;
            }
            Json sample = counters.Sample();
            AdlxValues board{};
            const bool hasAdlx = adlx.Read(board);
            if (!sample.is_object() && !hasAdlx) {
                continue;
            }
            const auto field = [&](const char* key) { return sample.is_object() ? sample.value(key, Json(nullptr)) : Json(nullptr); };
            std::lock_guard lock(sessionGpuMutex);
            sessionGpu.push_back({ perf::Now(), field("utilPct"), field("vramMb"), hasAdlx, board });
            if (sessionGpu.size() > 3600) {
                sessionGpu.pop_front();
            }
        }
    }
}

namespace devtools::profiler
{
    Json GpuCounters(double from, double to, double origin)
    {
        auto out = Json::array();
        std::lock_guard lock(sessionGpuMutex);
        for (const auto& sample : sessionGpu) {
            if (sample.at >= from && sample.at <= to) {
                out.push_back({ std::round((sample.at - origin) * 1e5) / 100.0, sample.utilPct, sample.vramMb });
            }
        }
        return out;
    }

    Json GpuMetrics(double from, double to, double origin)
    {
        Json out = adlx.Describe();
        auto rows = Json::array();
        {
            std::lock_guard lock(sessionGpuMutex);
            for (const auto& sample : sessionGpu) {
                if (sample.hasAdlx && sample.at >= from && sample.at <= to) {
                    auto row = AdlxRow(sample.adlx);
                    row.insert(row.begin(), std::round((sample.at - origin) * 1e5) / 100.0);
                    rows.push_back(std::move(row));
                }
            }
        }
        out["rows"] = std::move(rows);
        return out;
    }
}

DEVTOOLS_MODULE(ProfilerTop)
{
    std::thread(SampleSessionGpu).detach();
    Register("Profile.top", [](const Json& params) {
        AllowOnly(params, { "seconds", "threads", "gpu" });
        const double seconds = Optional<double>(params, "seconds", 3.0);
        if (seconds < 0.5 || seconds > 30.0) {
            throw RpcError(code::invalidParams, "seconds is 0.5 to 30");
        }
        const auto threadLimit = Optional<std::size_t>(params, "threads", 12);
        const double started = perf::Now();
        Json out = profiler::TopSamples(seconds);
        const double sampled = perf::Now();
        std::lock_guard lock(topStateMutex);
        const double now = perf::Now();
        out["memory"] = Memory(now);
        const double memoryDone = perf::Now();
        out["threads"] = Threads(memoryDone, threadLimit);
        const double threadsDone = perf::Now();
        const bool wantGpu = Optional<bool>(params, "gpu", true);
        out["gpu"] = wantGpu ? gpuCollector.Get(threadsDone) : Json(nullptr);
        if (wantGpu) {
            AdlxValues board{};
            if (adlx.Read(board)) {
                // PDH can be missing (old Windows, counters disabled) while the driver still reports the board.
                if (!out["gpu"].is_object()) {
                    out["gpu"] = { { "utilPct", nullptr }, { "engines", Json::object() }, { "vramMb", nullptr } };
                }
                static constexpr const char* names[] = DEVTOOLS_ADLX_NAMES;
                Json metrics = Json::object();
                for (std::size_t i = 0; i < board.size(); ++i) {
                    if (!std::isnan(board[i])) {
                        metrics[names[i]] = Round(board[i], 100.0);
                    }
                }
                Json described = adlx.Describe();
                out["gpu"]["board"] = { { "source", "adlx" }, { "gpu", described["gpu"] }, { "metrics", std::move(metrics) } };
            }
        }
        out["gpuTiming"] = profiler::GpuRecent(seconds);
        out["profiling"] = profiler::Profiling();
        const auto ms = [](double a, double b) { return Round((b - a) * 1000.0); };
        out["costMs"] = { { "samples", ms(started, sampled) }, { "memory", ms(now, memoryDone) },
            { "threads", ms(memoryDone, threadsDone) }, { "total", ms(started, perf::Now()) } };
        return out;
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(30000) });
}
