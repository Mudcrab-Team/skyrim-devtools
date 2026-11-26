// SPDX-License-Identifier: GPL-3.0-or-later
// Identity of the running plugin build, so a client can tell a stale DLL from a current one.
#pragma once

#include <string_view>

namespace devtools
{
    // "<git sha>[-dirty]:<hash of native/>" stamped by tools/pc-build; "unknown" when built by hand.
    [[nodiscard]] std::string_view BuildId();
}
