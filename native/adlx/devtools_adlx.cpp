// SPDX-License-Identifier: MIT
// Optional helper that reads AMD board metrics through ADLX (amdadlx64.dll, shipped with the Radeon driver).
// It is a separate DLL with a plain C ABI because the ADLX SDK headers carry AMD's own licence; the plugin
// never includes them and only loads this helper when it sits next to SkyrimDevTools.dll. Built only when
// ADLX_SDK points at a local ADLX checkout (native/CMakeLists.txt).
#include "devtools_adlx.h"

#include <Windows.h>

#include "ADLX.h"
#include "IPerformanceMonitoring.h"
#include "ISystem.h"

#include <cmath>
#include <cstring>
#include <limits>

using namespace adlx;

namespace
{
    HMODULE library = nullptr;
    ADLXTerminate_Fn terminate = nullptr;
    IADLXSystem* adlxSystem = nullptr;
    IADLXPerformanceMonitoringServicesPtr services;
    IADLXGPUPtr gpu;
    unsigned supported = 0;

    template <typename Fn>
    Fn Export(const char* name)
    {
        return reinterpret_cast<Fn>(GetProcAddress(library, name));
    }

    // An integrated GPU would hide the discrete card that renders the game, so a discrete one wins.
    IADLXGPUPtr PickGpu(IADLXGPUList* list)
    {
        IADLXGPUPtr first;
        for (adlx_uint i = list->Begin(); i != list->End(); ++i) {
            IADLXGPUPtr candidate;
            if (ADLX_FAILED(list->At(i, &candidate)) || !candidate) {
                continue;
            }
            ADLX_GPU_TYPE type = GPUTYPE_UNDEFINED;
            if (ADLX_SUCCEEDED(candidate->Type(&type)) && type == GPUTYPE_DISCRETE) {
                return candidate;
            }
            if (!first) {
                first = candidate;
            }
        }
        return first;
    }

    void Mark(unsigned bit, ADLX_RESULT (ADLX_STD_CALL IADLXGPUMetricsSupport::*check)(adlx_bool*), IADLXGPUMetricsSupport* support)
    {
        adlx_bool yes = false;
        if (ADLX_SUCCEEDED((support->*check)(&yes)) && yes) {
            supported |= 1u << bit;
        }
    }
}

extern "C" __declspec(dllexport) int DevtoolsAdlxVersion()
{
    return DEVTOOLS_ADLX_VERSION;
}

extern "C" __declspec(dllexport) int DevtoolsAdlxOpen(char* gpuName, int gpuNameSize, unsigned* supportedMask)
{
    if (adlxSystem) {
        return DEVTOOLS_ADLX_METRICS;
    }
    library = LoadLibraryW(ADLX_DLL_NAMEW);
    if (!library) {
        return -1;  // not an AMD driver
    }
    auto initialize = Export<ADLXInitialize_Fn>(ADLX_INIT_FUNCTION_NAME);
    auto initializeAny = Export<ADLXInitialize_Fn>(ADLX_INIT_WITH_INCOMPATIBLE_DRIVER_FUNCTION_NAME);
    terminate = Export<ADLXTerminate_Fn>(ADLX_TERMINATE_FUNCTION_NAME);
    ADLX_RESULT result = ADLX_FAIL;
    if (initialize) {
        result = initialize(ADLX_FULL_VERSION, &adlxSystem);
    }
    // A driver older than the SDK refuses the plain call but still serves the metrics it knows.
    if (ADLX_FAILED(result) && initializeAny) {
        result = initializeAny(ADLX_FULL_VERSION, &adlxSystem);
    }
    if (ADLX_FAILED(result) || !adlxSystem) {
        adlxSystem = nullptr;
        return -2;
    }
    IADLXGPUListPtr gpus;
    if (ADLX_FAILED(adlxSystem->GetGPUs(&gpus)) || !gpus || gpus->Empty()) {
        return -3;
    }
    gpu = PickGpu(gpus);
    if (!gpu || ADLX_FAILED(adlxSystem->GetPerformanceMonitoringServices(&services)) || !services) {
        return -4;
    }
    IADLXGPUMetricsSupportPtr support;
    if (ADLX_FAILED(services->GetSupportedGPUMetrics(gpu, &support)) || !support) {
        return -5;
    }
    Mark(0, &IADLXGPUMetricsSupport::IsSupportedGPUUsage, support);
    Mark(1, &IADLXGPUMetricsSupport::IsSupportedGPUClockSpeed, support);
    Mark(2, &IADLXGPUMetricsSupport::IsSupportedGPUVRAMClockSpeed, support);
    Mark(3, &IADLXGPUMetricsSupport::IsSupportedGPUTemperature, support);
    Mark(4, &IADLXGPUMetricsSupport::IsSupportedGPUHotspotTemperature, support);
    Mark(5, &IADLXGPUMetricsSupport::IsSupportedGPUPower, support);
    Mark(6, &IADLXGPUMetricsSupport::IsSupportedGPUTotalBoardPower, support);
    Mark(7, &IADLXGPUMetricsSupport::IsSupportedGPUFanSpeed, support);
    Mark(8, &IADLXGPUMetricsSupport::IsSupportedGPUVRAM, support);
    Mark(9, &IADLXGPUMetricsSupport::IsSupportedGPUVoltage, support);
    if (supportedMask) {
        *supportedMask = supported;
    }
    const char* name = nullptr;
    if (gpuName && gpuNameSize > 0) {
        gpuName[0] = '\0';
        if (ADLX_SUCCEEDED(gpu->Name(&name)) && name) {
            strncpy_s(gpuName, static_cast<size_t>(gpuNameSize), name, _TRUNCATE);
        }
    }
    return DEVTOOLS_ADLX_METRICS;
}

extern "C" __declspec(dllexport) int DevtoolsAdlxSample(double* values, int count)
{
    const double none = std::numeric_limits<double>::quiet_NaN();
    for (int i = 0; i < count; ++i) {
        values[i] = none;
    }
    if (!services || !gpu || count < DEVTOOLS_ADLX_METRICS) {
        return -1;
    }
    IADLXGPUMetricsPtr metrics;
    if (ADLX_FAILED(services->GetCurrentGPUMetrics(gpu, &metrics)) || !metrics) {
        return -2;
    }
    const auto real = [&](int index, ADLX_RESULT (ADLX_STD_CALL IADLXGPUMetrics::*get)(adlx_double*)) {
        adlx_double value = 0;
        if ((supported >> index & 1u) && ADLX_SUCCEEDED((metrics->*get)(&value))) {
            values[index] = value;
        }
    };
    const auto whole = [&](int index, ADLX_RESULT (ADLX_STD_CALL IADLXGPUMetrics::*get)(adlx_int*)) {
        adlx_int value = 0;
        if ((supported >> index & 1u) && ADLX_SUCCEEDED((metrics->*get)(&value))) {
            values[index] = value;
        }
    };
    real(0, &IADLXGPUMetrics::GPUUsage);
    whole(1, &IADLXGPUMetrics::GPUClockSpeed);
    whole(2, &IADLXGPUMetrics::GPUVRAMClockSpeed);
    real(3, &IADLXGPUMetrics::GPUTemperature);
    real(4, &IADLXGPUMetrics::GPUHotspotTemperature);
    real(5, &IADLXGPUMetrics::GPUPower);
    real(6, &IADLXGPUMetrics::GPUTotalBoardPower);
    whole(7, &IADLXGPUMetrics::GPUFanSpeed);
    whole(8, &IADLXGPUMetrics::GPUVRAM);
    whole(9, &IADLXGPUMetrics::GPUVoltage);
    return 0;
}

extern "C" __declspec(dllexport) void DevtoolsAdlxClose()
{
    services = nullptr;
    gpu = nullptr;
    adlxSystem = nullptr;
    if (terminate) {
        terminate();
    }
    terminate = nullptr;
    supported = 0;
    // amdadlx64.dll stays loaded: driver DLLs do not always tolerate being unloaded and reloaded.
}
