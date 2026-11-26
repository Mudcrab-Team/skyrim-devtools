// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the stuck detector; built and run by test_nav_graph.py.
#include "../native/stuck.h"

#include <cstdio>
#include <cstdlib>

using namespace devtools::nav::stuck;

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

    // Runs 4 Hz samples of a position function until a verdict arrives or `seconds` pass.
    template <class F>
    Verdict Run(Detector& d, double& now, double seconds, F pos, double remaining = -1.0, const DoorLog* doors = nullptr, double closing = 0.0)
    {
        const double end = now + seconds;
        while (now < end) {
            now += 0.25;
            const auto [x, y] = pos(now);
            const auto v = d.Update(now, x, y, remaining < 0.0 ? remaining : remaining - closing * now, doors);
            if (v.step != Step::none || v.unstuck) {
                return v;
            }
        }
        return {};
    }
}

int main()
{
    {
        // Pushing into a wall: detected within the window, escalates, aborts well inside 20 s.
        Detector d;
        double now = 0.0;
        auto wall = [](double) { return std::pair{ 100.0, 200.0 }; };
        auto v = Run(d, now, 5.0, wall);
        Check(v.step == Step::back && v.reason == Reason::stuck && now >= 3.0 && now <= 4.0, "first detection is a step back");
        Check(d.Recovering(now), "recovering after step back");
        now += 0.7;
        v = Run(d, now, 5.0, wall);
        Check(v.step == Step::jump, "second detection jumps");
        now += 1.0;
        v = Run(d, now, 5.0, wall);
        Check(v.step == Step::replan, "third detection replans");
        v = Run(d, now, 5.0, wall);
        Check(v.step == Step::abort && v.episodeS < 20.0 && v.failures == 4, "fourth detection aborts");
    }
    {
        // Moving normally never trips it.
        Detector d;
        double now = 0.0;
        const auto v = Run(d, now, 30.0, [](double t) { return std::pair{ t * 200.0, 0.0 }; });
        Check(v.step == Step::none, "running is not stuck");
    }
    {
        // Backing off and returning to the same wall is still one episode, and getting clear ends it.
        Detector d;
        double now = 0.0;
        auto v = Run(d, now, 5.0, [](double) { return std::pair{ 0.0, 0.0 }; });
        Check(v.step == Step::back, "episode starts");
        now += 0.7;
        const double t0 = now;
        v = Run(d, now, 10.0, [&](double t) { return std::pair{ (t - t0) * 200.0, 0.0 }; });
        Check(v.unstuck && v.failures == 1, "moving away ends the episode");
    }
    {
        // Zigzag along a wall: moves plenty but gains no distance to the goal.
        Detector d;
        double now = 0.0;
        auto zig = [](double t) { return std::pair{ std::fmod(t, 2.0) < 1.0 ? 0.0 : 150.0, 0.0 }; };
        const auto v = Run(d, now, 40.0, zig, 900.0);
        Check(v.reason == Reason::noProgress && v.step == Step::replan && now >= 15.0 && now <= 17.0, "no progress to the goal");
        const auto again = Run(d, now, 40.0, zig, 900.0);
        Check(again.step == Step::abort && again.reason == Reason::noProgress, "second no-progress aborts");
    }
    {
        // Improving remaining distance resets the clock.
        Detector d;
        double now = 0.0;
        const auto v = Run(d, now, 60.0, [](double t) { return std::pair{ t * 50.0, 0.0 }; }, 4000.0, nullptr, 50.0);
        Check(v.step == Step::none, "steady closing never trips");
    }
    {
        // Ping-pong between two spots with no goal.
        Detector d;
        double now = 0.0;
        auto pong = [](double t) { return std::pair{ std::fmod(t, 4.0) < 2.0 ? 600.0 * std::fmod(t, 2.0) / 2.0 : 600.0 - 600.0 * std::fmod(t, 2.0) / 2.0, 0.0 }; };
        const auto v = Run(d, now, 30.0, pong);
        Check(v.reason == Reason::oscillation && now <= 12.0, "oscillation");
    }
    {
        // A straight walk without a goal is fine.
        Detector d;
        double now = 0.0;
        const auto v = Run(d, now, 30.0, [](double t) { return std::pair{ t * 150.0, t * 30.0 }; });
        Check(v.step == Step::none, "straight walk is not oscillation");
    }
    {
        // The same door entered twice inside a minute.
        Detector d;
        DoorLog log;
        double now = 0.0;
        d.Reset(now);
        auto walk = [](double t) { return std::pair{ t * 100.0, 0.0 }; };
        log.Note(-30.0, 77);  // before the trip began: counts as history, not as a trigger
        auto v = Run(d, now, 10.0, walk, -1.0, &log);
        Check(v.step == Step::none, "history alone does not trigger");
        log.Note(now, 12);
        v = Run(d, now, 3.0, walk, -1.0, &log);
        Check(v.step == Step::none, "a different door does not trigger");
        log.Note(now, 77);
        v = Run(d, now, 3.0, walk, -1.0, &log);
        Check(v.reason == Reason::doorLoop && v.step == Step::replan && v.door == 77, "door loop");
    }
    {
        // Disabled: nothing happens.
        Config cfg;
        cfg.enabled = false;
        Detector d(cfg);
        double now = 0.0;
        const auto v = Run(d, now, 60.0, [](double) { return std::pair{ 0.0, 0.0 }; });
        Check(v.step == Step::none, "disabled");
    }
    if (failures == 0) {
        std::printf("ok\n");
    }
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
