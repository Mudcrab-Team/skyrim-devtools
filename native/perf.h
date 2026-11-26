// SPDX-License-Identifier: GPL-3.0-or-later
// Timing shared by the command bus and the frame monitor: a QPC clock and the slow-call log.
#pragma once

#include "core.h"

#include <string>

namespace devtools::perf
{
    // Seconds on the QPC clock; the same clock stamps frames, hitches and slow calls.
    [[nodiscard]] double Now();

    // A handler that held the game thread (or waited for it) long enough to matter for frame pacing.
    // Frame pacing over the last `seconds`: presents counted, and the longest gap between two of them.
    // Reads the frame ring under its lock, so it is cheap enough for a 2 Hz poller.
    struct RecentFrames
    {
        std::size_t count = 0;
        double maxMs = 0.0;
        bool hooked = false;
    };
    [[nodiscard]] RecentFrames Recent(double seconds);

    void NoteCall(const std::string& method, const char* thread, double startedAt, double queuedMs, double ranMs);

    // The 250 ms process counters inside [from, to] as [ms after origin, page faults, working set bytes,
    // read bytes, write bytes, CPU 100 ns, available physical bytes]; all but the time are running totals
    // or levels.
    Json CounterSeries(double from, double to, double origin);
}
