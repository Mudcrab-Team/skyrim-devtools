// SPDX-License-Identifier: GPL-3.0-or-later
// Game-independent stuck detection shared by Move.walk and Nav.goto. Plain walking into a wall,
// wall-sliding, ping-pong between two spots, a route that never gets closer and a door that keeps
// being re-entered all look different to a position sampler; this one watches for each and walks
// the caller up one escalation ladder. Kept free of game types so it can be tested off the PC.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace devtools::nav::stuck
{
    struct Config
    {
        bool enabled = true;
        double distance = 40.0;       // displacement under this over windowS while input is held: stuck
        double windowS = 3.0;
        int maxFailures = 3;          // recoveries tried before giving up
        double totalS = 20.0;         // hard cap on one stuck episode
        double progressWindowS = 10.0;  // goal distance must improve by progressGain within this; two windows end a trip, so 20 s
        double progressGain = 100.0;
        double oscWindowS = 10.0;     // goal-less movement: net displacement vs path walked
        double oscRatio = 0.2;
        double oscMinPath = 400.0;    // walked path below this is not evidence of anything
        int doorRepeat = 2;           // arrivals through the same door within doorWindowS
        double doorWindowS = 60.0;
    };

    enum class Reason { none, stuck, noProgress, oscillation, doorLoop };
    enum class Step { none, back, jump, replan, abort };

    inline const char* Name(Reason reason)
    {
        switch (reason) {
        case Reason::stuck: return "stuck";
        case Reason::noProgress: return "noProgress";
        case Reason::oscillation: return "oscillation";
        case Reason::doorLoop: return "doorLoop";
        default: return "none";
        }
    }

    inline const char* Name(Step step)
    {
        switch (step) {
        case Step::back: return "stepBackStrafe";
        case Step::jump: return "jump";
        case Step::replan: return "replan";
        case Step::abort: return "abort";
        default: return "none";
        }
    }

    // Which door the player arrived through and when; one log serves every detector.
    class DoorLog
    {
    public:
        void Note(double now, std::uint32_t id) { entries_.emplace_back(now, id); Trim(now); }
        // A door arrived through at least `repeat` times within `window` seconds, counting only when
        // one of those arrivals is newer than `newerThan`; its id, or 0.
        std::uint32_t Repeated(double now, double newerThan, int repeat, double window) const
        {
            for (const auto& [t, id] : entries_) {
                if (t <= newerThan) {
                    continue;
                }
                int count = 0;
                for (const auto& [t2, id2] : entries_) {
                    if (id2 == id && now - t2 <= window) {
                        ++count;
                    }
                }
                if (count >= repeat) {
                    return id;
                }
            }
            return 0;
        }

    private:
        void Trim(double now)
        {
            while (!entries_.empty() && now - entries_.front().first > 600.0) {
                entries_.pop_front();
            }
        }
        std::deque<std::pair<double, std::uint32_t>> entries_;
    };

    struct Verdict
    {
        Step step = Step::none;
        Reason reason = Reason::none;
        int failures = 0;      // detections in this episode so far
        double episodeS = 0.0;
        std::uint32_t door = 0;  // doorLoop: the door re-entered
        bool unstuck = false;  // an episode just ended well
        double unstuckAfterS = 0.0;
    };

    class Detector
    {
    public:
        explicit Detector(Config config = {}) : cfg_(config) {}

        const Config& Settings() const { return cfg_; }

        // Forget everything sampled so far (a load, a menu, a teleport, the end of a pause).
        void Reset(double now)
        {
            samples_.clear();
            walked_ = 0.0;
            bestAt_ = now;
            haveBest_ = false;
            doorSeen_ = now;
        }

        // Keep going from a new place without ending the episode (a recovery just moved the player).
        // Progress towards the goal is not restarted: a recovery that gets the player nowhere closer must not
        // buy another full window before the next verdict.
        void Rebase(double now)
        {
            (void)now;
            samples_.clear();
            walked_ = 0.0;
        }

        // A recovery the caller chooses itself, for steps with no meaning to it (replan on a fixed heading).
        void BeginRecovery(double now, Step step, double seconds)
        {
            step_ = step;
            recoverUntil_ = now + seconds;
            jumped_ = false;
        }

        [[nodiscard]] bool Recovering(double now) const { return now < recoverUntil_; }
        [[nodiscard]] Step RecoveryStep() const { return step_; }
        [[nodiscard]] int Failures() const { return failures_; }
        [[nodiscard]] Reason EpisodeReason() const { return reason_; }

        // Controls to hold while a recovery runs; `tapJump` is true on the poll that should press Jump.
        struct Controls
        {
            std::vector<std::string> hold;
            bool tapJump = false;
        };
        Controls RecoveryControls(double now)
        {
            Controls out;
            const bool left = failures_ % 2 == 1;
            if (step_ == Step::back) {
                out.hold = { "Back", left ? "Strafe Left" : "Strafe Right" };
            } else if (step_ == Step::jump) {
                out.hold = { "Forward" };
                if (!jumped_ || now - lastJump_ > 0.45) {
                    out.tapJump = true;
                    jumped_ = true;
                    lastJump_ = now;
                }
            }
            return out;
        }

        // One poll's sample. `remaining` is the distance still to go, negative when there is no goal.
        // `doors` is optional. Returns a verdict whose step is none unless a recovery or abort is due.
        Verdict Update(double now, double x, double y, double remaining, const DoorLog* doors = nullptr)
        {
            Verdict verdict;
            if (!cfg_.enabled || Recovering(now)) {
                return verdict;
            }
            Sample(now, x, y);
            if (remaining >= 0.0) {
                if (!haveBest_ || remaining < best_ - cfg_.progressGain) {
                    best_ = remaining;
                    haveBest_ = true;
                    bestAt_ = now;
                }
            }
            Reason found = Reason::none;
            std::uint32_t door = 0;
            if (doors && (door = doors->Repeated(now, doorSeen_, cfg_.doorRepeat, cfg_.doorWindowS))) {
                found = Reason::doorLoop;
                doorSeen_ = now;
            } else if (Pinned(now)) {
                found = Reason::stuck;
            } else if (remaining >= 0.0 && haveBest_ && now - bestAt_ >= cfg_.progressWindowS) {
                found = Reason::noProgress;
                bestAt_ = now;
            } else if (remaining < 0.0 && Oscillating(now)) {
                found = Reason::oscillation;
            }
            if (found == Reason::none) {
                if (failures_ > 0 && Released(now, x, y)) {
                    verdict.unstuck = true;
                    verdict.unstuckAfterS = now - episodeAt_;
                    verdict.failures = failures_;
                    failures_ = 0;
                    reason_ = Reason::none;
                }
                return verdict;
            }
            if (failures_ == 0) {
                episodeAt_ = now - (found == Reason::stuck ? cfg_.windowS : 0.0);
            }
            ++failures_;
            reason_ = found;
            lastDetectAt_ = now;
            verdict.reason = found;
            verdict.failures = failures_;
            verdict.door = door;
            verdict.episodeS = now - episodeAt_;
            const bool simple = found == Reason::stuck;
            const bool exhausted = simple ? failures_ > cfg_.maxFailures : failures_ > 1;
            if (exhausted || verdict.episodeS >= cfg_.totalS) {
                verdict.step = Step::abort;
                return verdict;
            }
            if (simple) {
                const Step ladder[] = { Step::back, Step::jump, Step::replan };
                step_ = ladder[(failures_ - 1) % 3];
            } else {
                step_ = Step::replan;
            }
            verdict.step = step_;
            if (step_ == Step::back) {
                recoverUntil_ = now + 0.6;
            } else if (step_ == Step::jump) {
                recoverUntil_ = now + 0.9;
                jumped_ = false;
            } else {
                recoverUntil_ = now;  // replanning is the caller's; nothing to hold
            }
            Rebase(now);
            return verdict;
        }

    private:
        struct Point
        {
            double t, x, y, walked;
        };

        void Sample(double now, double x, double y)
        {
            if (!samples_.empty() && now - samples_.back().t < 0.2) {
                return;
            }
            if (!samples_.empty()) {
                const double step = std::hypot(x - samples_.back().x, y - samples_.back().y);
                walked_ += step < 1000.0 ? step : 0.0;
            }
            samples_.push_back({ now, x, y, walked_ });
            const double keep = std::max(cfg_.windowS, cfg_.oscWindowS) + 1.0;
            while (samples_.size() > 2 && now - samples_.front().t > keep) {
                samples_.pop_front();
            }
        }

        // Every sample of the last windowS within `distance` of the newest, with the window fully covered.
        bool Pinned(double now) const
        {
            if (samples_.size() < 3 || now - samples_.front().t < cfg_.windowS) {
                return false;
            }
            const auto& last = samples_.back();
            for (const auto& s : samples_) {
                if (now - s.t <= cfg_.windowS && std::hypot(s.x - last.x, s.y - last.y) >= cfg_.distance) {
                    return false;
                }
            }
            return true;
        }

        // Plenty walked, little gained, over oscWindowS; or the same 100-unit cell re-entered again and again.
        bool Oscillating(double now) const
        {
            if (samples_.size() < 5 || now - samples_.front().t < cfg_.oscWindowS) {
                return false;
            }
            const Point* first = nullptr;
            for (const auto& s : samples_) {
                if (now - s.t <= cfg_.oscWindowS) {
                    first = &s;
                    break;
                }
            }
            if (!first) {
                return false;
            }
            const auto& last = samples_.back();
            const double path = last.walked - first->walked;
            if (path < cfg_.oscMinPath) {
                return false;
            }
            if (std::hypot(last.x - first->x, last.y - first->y) < cfg_.oscRatio * path) {
                return true;
            }
            std::map<std::pair<long long, long long>, int> entries;
            std::pair<long long, long long> previous{ 1LL << 40, 0 };
            for (const auto& s : samples_) {
                if (now - s.t > cfg_.oscWindowS) {
                    continue;
                }
                const std::pair<long long, long long> cell{ std::llround(std::floor(s.x / 100.0)), std::llround(std::floor(s.y / 100.0)) };
                if (cell != previous && ++entries[cell] >= 4) {
                    return true;
                }
                previous = cell;
            }
            return false;
        }

        // An episode ends well when a whole quiet spell passes without another detection: long enough for
        // a step back and the run back to the same wall to be flagged again. Distance from the first spot
        // is no test, because the recovery itself moves the player that far.
        bool Released(double now, double, double) const
        {
            if (Recovering(now)) {
                return false;
            }
            const double quiet = reason_ == Reason::stuck ? 2.0 * cfg_.windowS + 1.0 : std::max(cfg_.progressWindowS, cfg_.oscWindowS);
            return now - lastDetectAt_ > quiet;
        }

        Config cfg_;
        std::deque<Point> samples_;
        double walked_ = 0.0;
        double best_ = 0.0;
        bool haveBest_ = false;
        double bestAt_ = 0.0;
        double doorSeen_ = 0.0;  // arrivals up to here have been looked at
        int failures_ = 0;
        Reason reason_ = Reason::none;
        Step step_ = Step::none;
        double recoverUntil_ = 0.0;
        double episodeAt_ = 0.0;
        double lastDetectAt_ = 0.0;
        bool jumped_ = false;
        double lastJump_ = 0.0;
    };
}
