// SPDX-License-Identifier: GPL-3.0-or-later
// Thread names for the profiler and the trace. Windows keeps a thread's name only when it is set with
// SetThreadDescription; the older MSVC convention raises exception 0x406D1388 for a debugger to read and
// leaves nothing behind, so a vectored handler records those names from plugin load on. Threads named
// neither way are labelled by the client from their start address and their outermost game frame.
#pragma once

#include "core.h"

#include <Windows.h>

#include <cstdint>
#include <string>

namespace devtools::threads
{
    struct Name
    {
        std::string text;
        const char* source = "";  // "description", "exception" or "" when unnamed
    };

    // `thread` may be null; the description is then read through a handle opened here.
    Name NameOf(DWORD tid, HANDLE thread = nullptr);

    // The address the thread was created with (often a CRT or engine trampoline), 0 when unknown.
    std::uintptr_t StartAddress(HANDLE thread);

    // The outermost return address inside the executable on the calling thread's stack, 0 when none.
    std::uintptr_t RootGameFrame();

    // {"module", "rva"} of a code address, or null.
    Json Where(std::uintptr_t address);

    // name, nameSource, start and root fields for a thread's JSON record.
    void Describe(Json& out, DWORD tid, HANDLE thread, std::uintptr_t start, std::uintptr_t root, const Name& early = {});
}
