// SPDX-License-Identifier: GPL-3.0-or-later
// The plugin's own log, readable through Logs.tail with source "plugin".
#pragma once

#include <string_view>

namespace devtools::logs
{
    // Appends one line to the run directory's devtools.log and the in-memory tail. Any thread.
    void Plugin(std::string_view level, std::string_view message);
}
