// SPDX-License-Identifier: GPL-3.0-or-later
// The command bus: parses JSON-RPC calls, runs them on the requested game thread and enforces
// per-method timeouts without blocking the connection, so many calls can be in flight.
#pragma once

#include "core.h"
#include "server/server.h"

#include <memory>
#include <string>

namespace devtools::bus
{
    void Handle(const std::string& text, const std::shared_ptr<Outbox>& outbox);
    [[nodiscard]] Json Stats();
}
