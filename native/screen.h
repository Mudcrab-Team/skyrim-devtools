// SPDX-License-Identifier: GPL-3.0-or-later
// Hand-off from the Present hook (perf.cpp) to the screenshot module (screen.cpp).
#pragma once

#include <cstdint>

namespace devtools::screen
{
    // Called on the render thread immediately before the real Present, with the finished frame
    // still in the back buffer. Costs one relaxed atomic load when no capture is pending.
    void BeforePresent(void* swapChain, std::uint64_t frame);
}

namespace devtools::perf
{
    [[nodiscard]] std::uint64_t FrameNumber();
    [[nodiscard]] bool PresentHooked();
}
