// SPDX-License-Identifier: GPL-3.0-or-later
// Server-to-client notifications. A module declares a type once and emits it from any thread:
//
//     devtools::events::Declare("menuOpenClose", "A menu opened or closed");
//     devtools::events::Emit("menuOpenClose", {{"menu", name}, {"opening", true}});
//
// Emit is a cheap no-op unless the controller subscribed to that type with Events.subscribe.
#pragma once

#include "core.h"
#include "server/server.h"

#include <memory>
#include <string_view>

namespace devtools::events
{
    void Declare(std::string_view type, std::string_view description);
    [[nodiscard]] bool Subscribed(std::string_view type);
    void Emit(std::string_view type, Json data);

    void Attach(std::shared_ptr<Outbox> outbox);
    void Detach();
    // Closes the controller connection once everything queued for it has been written.
    void CloseConnection();
}
