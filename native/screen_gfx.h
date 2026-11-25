// SPDX-License-Identifier: GPL-3.0-or-later
// Backbuffer readback and image encoding. This unit uses the system D3D11 and WIC headers only,
// so it never shares a translation unit with the game headers.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace devtools::gfx
{
    struct Frame
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::uint32_t format = 0;  // DXGI_FORMAT of the copied backbuffer
        std::vector<std::uint8_t> pixels;  // tightly packed rows in that format
    };

    struct Encoded
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::vector<std::uint8_t> bytes;
    };

    // Copies the swap chain's current back buffer to CPU memory. Call it on the render thread
    // just before Present, while the finished frame is still the back buffer. Throws
    // std::runtime_error for formats it cannot read.
    [[nodiscard]] Frame ReadBackbuffer(void* swapChain);

    // Box-filters the frame down to at most maxWidth and encodes it as PNG or JPEG. Safe on any
    // thread.
    [[nodiscard]] Encoded Encode(const Frame& frame, std::uint32_t maxWidth, bool jpeg, int quality);

    [[nodiscard]] std::string Base64(const std::vector<std::uint8_t>& bytes);
}
