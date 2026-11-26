// SPDX-License-Identifier: MIT
// C ABI of the optional SkyrimDevToolsAdlx.dll helper. The plugin resolves these by name at runtime.
#pragma once

#define DEVTOOLS_ADLX_VERSION 1
#define DEVTOOLS_ADLX_DLL L"SkyrimDevToolsAdlx.dll"

// Values written by DevtoolsAdlxSample, in this order; NaN when the board or driver does not report one.
#define DEVTOOLS_ADLX_METRICS 10
#define DEVTOOLS_ADLX_NAMES                                                                                  \
    { "gpu_util_pct", "gpu_clock_mhz", "gpu_vram_clock_mhz", "gpu_temp_c", "gpu_hotspot_c", "gpu_power_w", \
        "gpu_board_power_w", "gpu_fan_rpm", "gpu_vram_mb", "gpu_voltage_mv" }

// int DevtoolsAdlxVersion(void)
typedef int (*DevtoolsAdlxVersionFn)();
// int DevtoolsAdlxOpen(char* gpuName, int gpuNameSize, unsigned* supportedMask): DEVTOOLS_ADLX_METRICS on
// success, negative when there is no AMD driver (-1) or ADLX refused (-2 and below).
typedef int (*DevtoolsAdlxOpenFn)(char*, int, unsigned*);
// int DevtoolsAdlxSample(double* values, int count): 0 on success.
typedef int (*DevtoolsAdlxSampleFn)(double*, int);
// void DevtoolsAdlxClose(void)
typedef void (*DevtoolsAdlxCloseFn)();
