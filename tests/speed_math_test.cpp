// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the time-skip and speed arithmetic; built and run by test_nav_graph.py.
#include "../native/speed_math.h"

#include <cmath>
#include <cstdio>

using namespace devtools::speed::math;

namespace
{
    int failures = 0;

    void Check(bool ok, const char* what)
    {
        if (!ok) {
            std::printf("FAIL %s\n", what);
            ++failures;
        }
    }

    bool Near(double a, double b, double eps = 1e-6) { return std::abs(a - b) < eps; }
}

int main()
{
    // A fresh game: the global is the fraction of the first day, raw counts finished days.
    Clock fresh{ 8.15, 8.15 / 24.0, 0.0, 0 };
    const auto travel = Skip(fresh, 5.9);
    Check(travel.rawExcludesToday, "raw is recognised as finished days");
    Check(travel.crossed == 0, "no midnight in a 6 hour trip");
    Check(Near(travel.after.raw, 0.0), "raw stays at finished days");
    Check(Near(RebuiltDaysPassed(travel.after, true), (8.15 + 5.9) / 24.0), "global rebuilt from raw matches the skip");

    // The 30 hour wait that read 0.5 days high: the rebuilt global must gain exactly 1.25 days.
    const auto wait = Skip(travel.after, 30.0);
    Check(wait.crossed == 1, "one midnight in 30 hours from 14:00");
    Check(Near(wait.after.hour, 20.05, 0.01), "hour wraps");
    Check(Near(wait.after.raw, 1.0), "raw gained the finished day");
    const double gained = RebuiltDaysPassed(wait.after, true) - RebuiltDaysPassed(travel.after, true);
    Check(Near(gained, 1.25), "30 hours is 1.25 days after the engine rebuilds the global");
    Check(Near(wait.after.daysPassed, travel.after.daysPassed + 1.25), "the global written now equals the rebuilt one");

    // Several midnights at once.
    const auto many = Skip(Clock{ 23.0, 4.0 + 23.0 / 24.0, 4.0, 4 }, 50.0);
    Check(many.crossed == 3 && many.after.midnights == 7, "50 hours from 23:00 crosses three midnights");
    Check(Near(RebuiltDaysPassed(many.after, many.rawExcludesToday), 4.0 + 23.0 / 24.0 + 50.0 / 24.0), "multi-day skip stays consistent");

    // raw that tracks the global itself keeps doing so.
    const auto total = Skip(Clock{ 10.0, 3.5, 3.5, 3 }, 12.0);
    Check(!total.rawExcludesToday && Near(total.after.raw, 4.0), "total-days raw grows by the skipped time");

    // Exactly at midnight both conventions look alike; finished days is assumed.
    const auto midnight = Skip(Clock{ 0.0, 2.0, 2.0, 2 }, 30.0);
    Check(midnight.rawExcludesToday && Near(RebuiltDaysPassed(midnight.after, true), 2.0 + 30.0 / 24.0), "skip from midnight");

    // Measured walk speed: ramp and a blocked stretch do not move the steady value.
    SpeedSamples samples;
    for (int i = 0; i < 100; ++i) {
        const double t = i * 0.05;
        const double v = t < 0.5 ? 400.0 * t / 0.5 : (t > 4.0 ? 0.0 : 1200.0);
        samples.Add(t, 0.05, v * 0.05);
    }
    Check(Near(samples.Steady(0.75), 1200.0), "steady speed ignores the ramp and a short stop at the end");
    Check(Near(samples.Peak(), 1200.0), "peak speed");
    SpeedSamples empty;
    Check(empty.Steady(0.5) == 0.0 && empty.Count() == 0, "no samples");

    // Fast navigation distances grow with speed and never shrink below the base values.
    Check(Near(SpeedFactor(100.0), 1.0) && Near(SpeedFactor(1200.0), 3.0) && Near(SpeedFactor(99999.0), 4.0), "speed factor bounds");
    Check(BrakeDistance(1200.0) > BrakeDistance(400.0) && BrakeDistance(0.0) == BrakeDistance(400.0), "brake distance grows with speed");
    Check(Near(LookAheadDistance(150.0, 1200.0), 450.0) && Near(LookAheadDistance(150.0, 200.0), 150.0), "look-ahead scales");
    Check(Near(OffPathLimit(200.0, 1200.0), 400.0) && Near(OffPathLimit(200.0, 200.0), 200.0), "off-path limit scales");

    if (failures == 0) {
        std::printf("ok\n");
    }
    return failures == 0 ? 0 : 1;
}
