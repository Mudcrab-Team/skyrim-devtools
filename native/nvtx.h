// SPDX-License-Identifier: GPL-3.0-or-later
// NVTX ranges and markers for NVIDIA Nsight Systems captures, through nvToolsExt64_1.dll when it can be
// loaded. Without that DLL every call is a cheap no-op, so other vendors' machines pay nothing.
#pragma once

#include <cstdint>
#include <string_view>

namespace devtools::nvtx
{
    // A range that may end on another thread; 0 when NVTX is absent.
    std::uint64_t RangeStart(std::string_view name);
    void RangeEnd(std::uint64_t id);
    void Mark(std::string_view name);
    bool Available();
}
