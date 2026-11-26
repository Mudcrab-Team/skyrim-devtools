// SPDX-License-Identifier: GPL-3.0-or-later
// The id lives in a header that tools/pc-build generates beside the sources on the build host, so the
// repository never carries a value that would be out of date the moment it is committed.
#include "build_info.h"

#if __has_include("build_id.h")
#include "build_id.h"
#endif

#ifndef DEVTOOLS_BUILD_ID
#define DEVTOOLS_BUILD_ID "unknown"
#endif

namespace devtools
{
    std::string_view BuildId()
    {
        return DEVTOOLS_BUILD_ID;
    }
}
