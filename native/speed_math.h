// SPDX-License-Identifier: GPL-3.0-or-later
// Game-free arithmetic behind Game.waitHours, the measured walk speed and Nav.goto's fast mode, so it can be
// tested off the PC (tests/speed_math_test.cpp).
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace devtools::speed::math
{
    // The calendar counters a time skip has to keep consistent.
    struct Clock
    {
        double hour = 0.0;         // gameHour, 0-24
        double daysPassed = 0.0;   // the gameDaysPassed global
        double raw = 0.0;          // Calendar::rawDaysPassed
        std::uint32_t midnights = 0;
    };

    struct Skipped
    {
        Clock after;
        std::uint32_t crossed = 0;   // midnights passed during the skip
        bool rawExcludesToday = false;
    };

    // The engine rebuilds the daysPassed global from rawDaysPassed plus the fraction of the current day, so raw is
    // a count of finished days that must grow by the midnights crossed; writing the hour fraction into it counted
    // the fraction twice (the global read back 0.5 days high after a 30 hour skip). When raw instead tracks the
    // global itself it grows by the skipped time. Either way raw + fraction-of-day (or raw alone) stays equal to
    // the global.
    inline Skipped Skip(const Clock& before, double hours)
    {
        Skipped out;
        const double sum = before.hour + hours;
        out.crossed = static_cast<std::uint32_t>(std::floor(sum / 24.0));
        out.after.hour = std::fmod(sum, 24.0);
        out.after.daysPassed = before.daysPassed + hours / 24.0;
        out.after.midnights = before.midnights + out.crossed;
        const double fraction = before.hour / 24.0;
        // Finished days is the convention seen in the game; only a raw that equals the global while the day has a
        // fraction to account for is taken as a running total.
        const bool runningTotal = std::abs(before.daysPassed - before.raw) < 0.01 && fraction > 0.02;
        out.rawExcludesToday = !runningTotal;
        out.after.raw = out.rawExcludesToday ? before.raw + out.crossed : before.raw + hours / 24.0;
        return out;
    }

    // What the global reads once the engine has rebuilt it from the counters.
    inline double RebuiltDaysPassed(const Clock& clock, bool rawExcludesToday)
    {
        return rawExcludesToday ? clock.raw + clock.hour / 24.0 : clock.raw;
    }

    // Horizontal speed samples from a walk: units per second between polls.
    class SpeedSamples
    {
    public:
        void Add(double elapsed, double dt, double step)
        {
            if (dt > 1e-4) {
                times_.push_back(elapsed);
                speeds_.push_back(step / dt);
            }
        }

        // Median after `rampS` (the player accelerates and the engine adopts a new walk speed over a moment);
        // an obstacle that stops the player for a part of the walk does not move a median.
        double Steady(double rampS) const
        {
            std::vector<double> kept;
            for (std::size_t i = 0; i < speeds_.size(); ++i) {
                if (times_[i] >= rampS) {
                    kept.push_back(speeds_[i]);
                }
            }
            if (kept.empty()) {
                kept = speeds_;
            }
            return Median(kept);
        }

        double Peak() const { return speeds_.empty() ? 0.0 : *std::max_element(speeds_.begin(), speeds_.end()); }
        std::size_t Count() const { return speeds_.size(); }

    private:
        static double Median(std::vector<double> values)
        {
            if (values.empty()) {
                return 0.0;
            }
            std::sort(values.begin(), values.end());
            const auto mid = values.size() / 2;
            return values.size() % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2.0;
        }

        std::vector<double> times_;
        std::vector<double> speeds_;
    };

    // Fast navigation. The route follower reaches waypoints, rounds gentle bends and measures deviation in fixed
    // distances, and the player turns at a fixed rate; at three times the walk speed all of those distances are
    // crossed three times as fast, so a corner is overshot, the player drifts off the route and the planner
    // replans (the 300% run replanned as often as the 100% one and was slower). The follower therefore slows to
    // normal speed ahead of a sharp corner and widens its distances with the speed it is actually moving at.
    constexpr double normalRunSpeed = 400.0;  // units per second at 100%, the scale the widening is relative to

    inline double SpeedFactor(double unitsPerSecond) { return std::clamp(unitsPerSecond / normalRunSpeed, 1.0, 4.0); }

    // Distance before a sharp corner at which the boost is dropped: the walk speed takes a moment to settle.
    inline double BrakeDistance(double unitsPerSecond) { return 150.0 + 0.7 * std::max(unitsPerSecond, normalRunSpeed); }

    inline double LookAheadDistance(double base, double unitsPerSecond) { return base * SpeedFactor(unitsPerSecond); }

    inline double OffPathLimit(double base, double unitsPerSecond) { return base * (0.5 + 0.5 * SpeedFactor(unitsPerSecond)); }

    // A gentle bend may be rounded by more at speed; a sharp corner keeps its tight radius because the boost is off there.
    inline double GentleReach(double base, double unitsPerSecond) { return base * SpeedFactor(unitsPerSecond); }
}
