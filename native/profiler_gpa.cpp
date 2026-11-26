// SPDX-License-Identifier: GPL-3.0-or-later
// Opt-in AMD hardware counters through GPUPerfAPI (GPA 4.x, MIT). The GPA DLLs are not shipped: the caller
// names a local folder that holds GPUPerfAPIDX11-x64.dll and GPUPerfAPICounters-x64.dll, and nothing is
// loaded until the first request. A capture takes one frame per GPA pass, because the hardware can only
// count a few counters at once and GPA replays the same samples once per counter group. Each frame is cut
// into samples at the frame start and at every render-target or depth clear (the same pass boundaries as
// GPU timing). Pass 0 fixes the sample count; later frames are padded or have their extra clears merged into
// the last sample, so values are a composite of consecutive frames and want a steady scene.
// Every GPA call runs on the render thread from the Present detour and the clear hooks.
#include "gpu_performance_api/gpu_perf_api.h"

#include "logs.h"
#include "methods.h"
#include "perf.h"
#include "profiler.h"

#include <Windows.h>
#include <d3d11.h>
#include <dxgi.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace devtools::profiler
{
    namespace
    {
        constexpr int maxSamples = 32;
        constexpr int baselineFrames = 60;

        // A default set that answers "what is the GPU busy with" on RDNA; names missing on a device are skipped.
        const std::vector<std::string> defaultCounters{ "GPUTime", "GPUBusy", "VsGsBusy", "PSBusy", "CSBusy", "TexUnitBusy",
            "DepthStencilTestBusy", "PrimitiveAssemblyBusy", "PAStalledOnRasterizer", "PSExportStalls", "L0CacheHit", "L2CacheHit",
            "ScalarCacheHit", "InstCacheHit", "FetchSize", "WriteSize", "MemUnitBusy", "MemUnitStalled", "CulledPrims",
            "PrimitivesIn", "PSPixelsOut", "HiZQuadsCulled", "PreZQuadsCulled" };

        enum class Kind
        {
            List,
            Capture
        };

        enum class Phase
        {
            Idle,
            Baseline,
            Passes,
            Collecting
        };

        struct Request
        {
            Kind kind;
            std::wstring dir;
            std::vector<std::string> counters;
            int maxFrames;
            ReplyPtr reply;
        };

        std::mutex requestMutex;
        std::optional<Request> pending;
        std::atomic_bool active = false;      // a request waits or a capture runs; the Present detour checks this
        std::atomic_bool wantClears = false;  // a pass frame is open

        std::mutex logMutex;
        std::vector<std::string> gpaLog;

        // Render-thread state.
        HMODULE library = nullptr;
        std::wstring libraryDir;
        GpaFunctionTable gpa;
        bool initialized = false;
        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* immediate = nullptr;
        GpaContextId gpaContext = nullptr;
        GpaSessionId session = nullptr;
        GpaCommandListId commandList = nullptr;
        std::optional<Request> job;
        Phase phase = Phase::Idle;
        int baselineLeft = 0;
        int framesSeen = 0;
        GpaUInt32 passCount = 0;
        GpaUInt32 pass = 0;
        bool inFrame = false;
        bool frameMismatch = false;
        int sampleCount = 0;
        int sampleIndex = 0;
        std::vector<std::string> sampleLabels;
        int mismatchedFrames = 0;
        int paddedSamples = 0;
        int mergedClears = 0;
        double gpaSeconds = 0.0;
        double initSeconds = 0.0;
        double lastPresent = 0.0;
        double captureStarted = 0.0;
        bool sessionEnded = false;
        bool skipInterval = false;
        std::vector<double> baselineMs;
        std::vector<double> captureMs;
        std::vector<std::string> enabledNames;
        std::vector<std::string> missingNames;

        void OnGpaLog(GpaLoggingType, const char* message)
        {
            std::lock_guard lock(logMutex);
            if (gpaLog.size() < 40 && message) {
                gpaLog.emplace_back(message);
            }
        }

        std::string Status(GpaStatus status)
        {
            const char* text = gpa.GpaGetStatusAsStr ? gpa.GpaGetStatusAsStr(status) : nullptr;
            return text ? text : "status " + std::to_string(static_cast<int>(status));
        }

        std::string Narrow(const std::wstring& text)
        {
            const int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
            std::string out(static_cast<std::size_t>(std::max(size, 1) - 1), '\0');
            WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, out.data(), size, nullptr, nullptr);
            return out;
        }

        double Median(std::vector<double> values)
        {
            if (values.empty()) {
                return 0.0;
            }
            std::sort(values.begin(), values.end());
            return values[values.size() / 2];
        }

        double Rounded(double value, double scale = 1000.0)
        {
            return std::round(value * scale) / scale;
        }

        // Loads GPA from `dir` once per folder; GPA stays loaded because unloading it under a live device is unsafe.
        std::string Load(const std::wstring& dir)
        {
            if (library) {
                return libraryDir == dir ? std::string() : "GPUPerfAPI is already loaded from " + Narrow(libraryDir);
            }
            std::wstring path = dir;
            if (!path.empty() && path.back() != L'\\' && path.back() != L'/') {
                path += L'\\';
            }
            // With an absolute path this flag makes Windows find GPUPerfAPICounters-x64.dll in the same folder.
            library = LoadLibraryExW((path + L"GPUPerfAPIDX11-x64.dll").c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
            if (!library) {
                return "GPUPerfAPIDX11-x64.dll not found in " + Narrow(dir) + " (error " + std::to_string(GetLastError()) + ")";
            }
            libraryDir = dir;
            const auto getTable = reinterpret_cast<GpaGetFuncTablePtrType>(GetProcAddress(library, "GpaGetFuncTable"));
            if (!getTable) {
                return "GPUPerfAPIDX11-x64.dll has no GpaGetFuncTable";
            }
            const GpaStatus status = getTable(&gpa);
            if (status != kGpaStatusOk) {
                return "GPUPerfAPI function table refused (" + std::to_string(static_cast<int>(status)) + "); GPA 4.x is required";
            }
            gpa.GpaRegisterLoggingCallback(kGpaLoggingError, OnGpaLog);
            return {};
        }

        // GPA, a context on the game's device and an empty session.
        std::string Open()
        {
            if (std::string error = Load(job->dir); !error.empty()) {
                return error;
            }
            if (!initialized) {
                const GpaStatus status = gpa.GpaInitialize(kGpaInitializeDefaultBit);
                if (status != kGpaStatusOk) {
                    return "GpaInitialize: " + Status(status);
                }
                initialized = true;
            }
            // Clocks are left alone so the game runs as it does unprofiled; counters then vary with boost state.
            GpaStatus status = gpa.GpaOpenContext(device, kGpaOpenContextClockModeNoneBit, &gpaContext);
            if (status != kGpaStatusOk) {
                gpaContext = nullptr;
                return "GpaOpenContext on the game's D3D11 device: " + Status(status);
            }
            status = gpa.GpaCreateSession(gpaContext, kGpaSessionSampleTypeDiscreteCounter, &session);
            if (status != kGpaStatusOk) {
                session = nullptr;
                return "GpaCreateSession: " + Status(status);
            }
            return {};
        }

        void Close()
        {
            if (session) {
                gpa.GpaDeleteSession(session);
                session = nullptr;
            }
            if (gpaContext) {
                gpa.GpaCloseContext(gpaContext);
                gpaContext = nullptr;
            }
            if (initialized) {
                gpa.GpaDestroy();
                initialized = false;
            }
        }

        std::string DeviceName()
        {
            const char* name = nullptr;
            return gpaContext && gpa.GpaGetDeviceName(gpaContext, &name) == kGpaStatusOk && name ? name : "";
        }

        Json Logged()
        {
            std::lock_guard lock(logMutex);
            Json out = gpaLog;
            gpaLog.clear();
            return out;
        }

        void Finish(Json result)
        {
            wantClears = false;
            inFrame = false;
            Close();
            phase = Phase::Idle;
            if (!result.contains("log")) {
                result["log"] = Logged();
            }
            if (job && job->reply) {
                job->reply->Ok(std::move(result));
            }
            job.reset();
            std::lock_guard lock(requestMutex);
            active = pending.has_value();
        }

        void Fail(const std::string& error)
        {
            logs::Plugin("warn", "profiler: GPUPerfAPI " + error);
            Finish(Json{ { "ok", false }, { "error", error }, { "device", DeviceName() } });
        }

        const char* UsageName(GpaUsageType usage)
        {
            switch (usage) {
            case kGpaUsageTypeRatio: return "ratio";
            case kGpaUsageTypePercentage: return "percentage";
            case kGpaUsageTypeCycles: return "cycles";
            case kGpaUsageTypeMilliseconds: return "milliseconds";
            case kGpaUsageTypeBytes: return "bytes";
            case kGpaUsageTypeItems: return "items";
            case kGpaUsageTypeKilobytes: return "kilobytes";
            case kGpaUsageTypeNanoseconds: return "nanoseconds";
            default: return "other";
            }
        }

        Json CounterInfo(GpaUInt32 index)
        {
            const char* name = nullptr;
            const char* group = nullptr;
            const char* description = nullptr;
            GpaUsageType usage = kGpaUsageTypeLast;
            gpa.GpaGetCounterName(session, index, &name);
            gpa.GpaGetCounterGroup(session, index, &group);
            gpa.GpaGetCounterDescription(session, index, &description);
            gpa.GpaGetCounterUsageType(session, index, &usage);
            return Json{ { "name", name ? name : "" }, { "group", group ? group : "" }, { "usage", UsageName(usage) },
                { "description", description ? description : "" } };
        }

        // Enables what exists of `names`; returns the GPA pass count.
        GpaUInt32 Enable(const std::vector<std::string>& names)
        {
            enabledNames.clear();
            missingNames.clear();
            for (const auto& name : names) {
                if (gpa.GpaEnableCounterByName(session, name.c_str()) == kGpaStatusOk) {
                    enabledNames.push_back(name);
                } else {
                    missingNames.push_back(name);
                }
            }
            GpaUInt32 passes = 0;
            gpa.GpaGetPassCount(session, &passes);
            return passes;
        }

        void ServeList()
        {
            if (std::string error = Open(); !error.empty()) {
                Fail(error);
                return;
            }
            GpaUInt32 count = 0;
            gpa.GpaGetNumCounters(session, &count);
            auto counters = Json::array();
            for (GpaUInt32 i = 0; i < count; ++i) {
                counters.push_back(CounterInfo(i));
            }
            const GpaUInt32 passes = Enable(defaultCounters);
            Finish(Json{ { "ok", true }, { "device", DeviceName() }, { "counters", std::move(counters) },
                { "defaultCounters", enabledNames }, { "defaultMissing", missingNames }, { "defaultPassCount", passes } });
        }

        void StartCapture()
        {
            const double started = perf::Now();
            if (std::string error = Open(); !error.empty()) {
                Fail(error);
                return;
            }
            passCount = Enable(job->counters.empty() ? defaultCounters : job->counters);
            if (enabledNames.empty() || !passCount) {
                Fail("none of the requested counters exist on this device");
                return;
            }
            const GpaStatus status = gpa.GpaBeginSession(session);
            if (status != kGpaStatusOk) {
                Fail("GpaBeginSession: " + Status(status));
                return;
            }
            EnsurePassHooks(immediate);
            captureStarted = perf::Now();
            pass = 0;
            sampleCount = 0;
            sampleLabels.clear();
            mismatchedFrames = paddedSamples = mergedClears = 0;
            gpaSeconds = 0.0;
            captureMs.clear();
            phase = Phase::Passes;
            initSeconds = perf::Now() - started;
            skipInterval = true;  // the frame that opened GPA carries its start-up cost, reported apart
        }

        bool Check(GpaStatus status, const char* what)
        {
            if (status == kGpaStatusOk) {
                return true;
            }
            Fail(std::string(what) + ": " + Status(status));
            return false;
        }

        void NextSample(std::string label)
        {
            if (!Check(gpa.GpaEndSample(commandList), "GpaEndSample") ||
                !Check(gpa.GpaBeginSample(static_cast<GpaUInt32>(++sampleIndex), commandList), "GpaBeginSample")) {
                return;
            }
            if (pass == 0) {
                sampleLabels.push_back(std::move(label));
            }
        }

        void BeginPassFrame()
        {
            if (!Check(gpa.GpaBeginCommandList(session, pass, GPA_NULL_COMMAND_LIST, kGpaCommandListNone, &commandList), "GpaBeginCommandList")) {
                return;
            }
            sampleIndex = 0;
            frameMismatch = false;
            if (!Check(gpa.GpaBeginSample(0, commandList), "GpaBeginSample")) {
                return;
            }
            if (pass == 0) {
                sampleLabels.assign(1, "frame start");
            }
            inFrame = true;
            wantClears = true;
        }

        void EndPassFrame()
        {
            wantClears = false;
            inFrame = false;
            // Every pass must carry the same sample ids as pass 0.
            while (pass > 0 && sampleIndex + 1 < sampleCount && phase == Phase::Passes) {
                ++paddedSamples;
                frameMismatch = true;
                NextSample({});
            }
            if (phase != Phase::Passes || !Check(gpa.GpaEndSample(commandList), "GpaEndSample") ||
                !Check(gpa.GpaEndCommandList(commandList), "GpaEndCommandList")) {
                return;
            }
            if (pass == 0) {
                sampleCount = sampleIndex + 1;
            }
            mismatchedFrames += frameMismatch ? 1 : 0;
            if (++pass >= passCount) {
                phase = Phase::Collecting;
            }
        }

        // Reads every sample once the session is complete.
        void Collect()
        {
            Json result{ { "ok", true } };
            GpaUInt32 enabled = 0;
            gpa.GpaGetNumEnabledCounters(session, &enabled);
            std::vector<GpaUInt32> indices(enabled);
            std::vector<GpaDataType> types(enabled, kGpaDataTypeUint64);
            auto counters = Json::array();
            for (GpaUInt32 i = 0; i < enabled; ++i) {
                gpa.GpaGetEnabledIndex(session, i, &indices[i]);
                gpa.GpaGetCounterDataType(session, indices[i], &types[i]);
                counters.push_back(CounterInfo(indices[i]));
                counters.back()["values"] = Json::array();
            }
            for (int s = 0; s < sampleCount; ++s) {
                std::size_t size = 0;
                std::vector<GpaUInt64> buffer;
                if (gpa.GpaGetSampleResultSize(session, static_cast<GpaUInt32>(s), &size) == kGpaStatusOk && size) {
                    buffer.resize((size + sizeof(GpaUInt64) - 1) / sizeof(GpaUInt64));
                    if (gpa.GpaGetSampleResult(session, static_cast<GpaUInt32>(s), size, buffer.data()) != kGpaStatusOk) {
                        buffer.clear();
                    }
                }
                for (GpaUInt32 i = 0; i < enabled; ++i) {
                    Json value = nullptr;
                    if (i < buffer.size()) {
                        if (types[i] == kGpaDataTypeFloat64) {
                            double real = 0.0;
                            std::memcpy(&real, &buffer[i], sizeof(real));
                            value = Rounded(real, 1e4);
                        } else {
                            value = buffer[i];
                        }
                    }
                    counters[i]["values"].push_back(std::move(value));
                }
            }
            const double baseline = Median(baselineMs);
            const double capture = Median(captureMs);
            const double frames = static_cast<double>(std::max<std::size_t>(captureMs.size(), 1));
            result["device"] = DeviceName();
            result["passCount"] = passCount;
            result["framesUsed"] = pass;
            result["sampleCount"] = sampleCount;
            result["samples"] = sampleLabels;
            result["counters"] = std::move(counters);
            result["missingCounters"] = missingNames;
            result["mismatchedFrames"] = mismatchedFrames;
            result["paddedSamples"] = paddedSamples;
            result["mergedClears"] = mergedClears;
            result["passHooks"] = PassHooksOk();
            result["overhead"] = { { "initMs", Rounded(initSeconds * 1000.0, 100.0) },
                { "cpuUsPerFrame", Rounded(gpaSeconds * 1e6 / frames, 10.0) }, { "baselineFrameMsP50", Rounded(baseline, 100.0) },
                { "captureFrameMsP50", Rounded(capture, 100.0) },
                { "baselineFps", baseline > 0.0 ? Json(Rounded(1000.0 / baseline, 10.0)) : Json(nullptr) },
                { "captureFps", capture > 0.0 ? Json(Rounded(1000.0 / capture, 10.0)) : Json(nullptr) } };
            Mark("gpa", "GPUPerfAPI capture", captureStarted, perf::Now());
            Finish(std::move(result));
        }

    }

    bool GpaWantsClears()
    {
        return wantClears.load(std::memory_order_relaxed);
    }

    void GpaOnClear(void* context, const char* kind, void* view)
    {
        if (context != immediate || !inFrame || phase != Phase::Passes) {
            return;
        }
        const double started = perf::Now();
        if (sampleIndex + 1 < (pass == 0 ? maxSamples : sampleCount)) {
            NextSample(pass == 0 ? ViewLabel(kind, view) : std::string());
        } else {
            ++mergedClears;
            frameMismatch = frameMismatch || pass > 0;
        }
        gpaSeconds += perf::Now() - started;
    }

    void GpaBeforePresent(void* swapChainPointer)
    {
        if (!active.load(std::memory_order_relaxed)) {
            return;
        }
        const double started = perf::Now();
        if (!device) {
            auto* swapChain = static_cast<IDXGISwapChain*>(swapChainPointer);
            if (FAILED(swapChain->GetDevice(__uuidof(ID3D11Device), reinterpret_cast<void**>(&device))) || !device) {
                device = nullptr;
                if (!job) {
                    std::lock_guard lock(requestMutex);
                    job = std::move(pending);
                    pending.reset();
                }
                if (job) {
                    Fail("the swap chain has no D3D11 device");
                }
                return;
            }
            device->GetImmediateContext(&immediate);
        }
        if (lastPresent > 0.0 && !skipInterval) {
            const double ms = (started - lastPresent) * 1000.0;
            if (phase == Phase::Baseline) {
                baselineMs.push_back(ms);
            } else if (phase == Phase::Passes || phase == Phase::Collecting) {
                captureMs.push_back(ms);
            }
        }
        skipInterval = false;
        lastPresent = started;
        if (phase == Phase::Passes && inFrame) {
            EndPassFrame();
        }
        if (phase == Phase::Collecting) {
            if (!sessionEnded) {
                bool all = true;
                for (GpaUInt32 p = 0; p < passCount && all; ++p) {
                    all = gpa.GpaIsPassComplete(session, p) == kGpaStatusOk;
                }
                if (all) {
                    sessionEnded = Check(gpa.GpaEndSession(session), "GpaEndSession");
                }
            }
            if (sessionEnded && phase == Phase::Collecting && gpa.GpaIsSessionComplete(session) == kGpaStatusOk) {
                sessionEnded = false;
                Collect();
            }
        }
        if (phase == Phase::Passes || phase == Phase::Collecting) {
            gpaSeconds += perf::Now() - started;
        }
    }

    void GpaAfterPresent()
    {
        if (!active.load(std::memory_order_relaxed) || !device) {
            return;
        }
        const double started = perf::Now();
        if (phase == Phase::Idle) {
            {
                std::lock_guard lock(requestMutex);
                job = std::move(pending);
                pending.reset();
            }
            if (!job) {
                active = false;
                return;
            }
            if (job->kind == Kind::List) {
                ServeList();
                return;
            }
            phase = Phase::Baseline;
            baselineLeft = baselineFrames;
            framesSeen = 0;
            baselineMs.clear();
            sessionEnded = false;
        }
        if (phase == Phase::Baseline && --baselineLeft <= 0) {
            StartCapture();
        }
        if (phase == Phase::Passes) {
            BeginPassFrame();
        }
        if (phase != Phase::Idle && phase != Phase::Baseline && ++framesSeen > job->maxFrames) {
            if (inFrame) {
                gpa.GpaEndSample(commandList);
                gpa.GpaEndCommandList(commandList);
                inFrame = false;
            }
            Fail("no result after " + std::to_string(job->maxFrames) + " frames (" + std::to_string(pass) + " of " +
                 std::to_string(passCount) + " passes)");
            return;
        }
        if (phase == Phase::Passes || phase == Phase::Collecting) {
            gpaSeconds += perf::Now() - started;
        }
    }
}

DEVTOOLS_MODULE(ProfilerGpa)
{
    using namespace devtools;
    using namespace devtools::profiler;
    const auto submit = [](Kind kind, const Json& params, ReplyPtr reply) {
        const auto dir = Require<std::string>(params, "dir");
        Request request{ kind, {}, {}, 0, std::move(reply) };
        const int size = MultiByteToWideChar(CP_UTF8, 0, dir.c_str(), -1, nullptr, 0);
        request.dir.resize(static_cast<std::size_t>(std::max(size, 1) - 1));
        MultiByteToWideChar(CP_UTF8, 0, dir.c_str(), -1, request.dir.data(), size);
        if (params.contains("counters") && !params["counters"].is_null()) {
            request.counters = params["counters"].get<std::vector<std::string>>();
        }
        request.maxFrames = Optional<int>(params, "maxFrames", 600);
        if (request.maxFrames < 10 || request.maxFrames > 5000) {
            throw RpcError(code::invalidParams, "maxFrames is 10 to 5000");
        }
        std::lock_guard lock(requestMutex);
        if (pending || active) {
            throw RpcError(code::invalidParams, "a GPUPerfAPI request is already running");
        }
        pending = std::move(request);
        active = true;
    };
    RegisterDeferred("Profile.gpaCounters", [submit](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "dir" });
        submit(Kind::List, params, std::move(reply));
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(60000) });
    RegisterDeferred("Profile.gpaCapture", [submit](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "dir", "counters", "maxFrames" });
        submit(Kind::Capture, params, std::move(reply));
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(120000) });
}
