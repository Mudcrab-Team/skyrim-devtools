// SPDX-License-Identifier: GPL-3.0-or-later
// Timing shared by the command bus and the frame monitor: a QPC clock and the slow-call log.
#pragma once

#include <string>

namespace devtools::perf
{
    // Seconds on the QPC clock; the same clock stamps frames, hitches and slow calls.
    [[nodiscard]] double Now();

    // A handler that held the game thread (or waited for it) long enough to matter for frame pacing.
    void NoteCall(const std::string& method, const char* thread, double startedAt, double queuedMs, double ranMs);
}
