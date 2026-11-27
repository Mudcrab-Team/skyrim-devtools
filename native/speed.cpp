// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Walk speed multiplier, clock scale and skipping time. The SpeedMult actor value is 100 at normal
// speed; an automatic boost (Nav.goto fast) and a manual setting (Game.setSpeed) share one owner so
// the original value is restored exactly once.
#include "speed.h"

#include "events.h"
#include "speed_math.h"
#include "state_common.h"

#include <algorithm>
#include <cmath>

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    constexpr auto kSpeed = RE::ActorValue::kSpeedMult;

    float original = 100.0F;  // the value before any override
    bool overridden = false;
    float manual = 0.0F;      // Game.setSpeed walk, 0 when unset
    float boost = 0.0F;       // automatic boost, 0 when off
    float applied = 0.0F;

    struct Observed
    {
        double unitsPerSecond = 0.0;
        float percent = 100.0F;
        bool valid = false;
    } observed;  // the last measured steady walk, so the API can say what the engine actually delivered

    float tempDelta = 0.0F;  // the temporary modifier this module has put on SpeedMult

    // A temporary modifier goes through the same path as a spell effect, so the movement code's cached
    // speed is recomputed; writing the base value alone left it stale on every other change.
    void Write(float value)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        const auto values = player->AsActorValueOwner();
        const float wanted = value - original;
        const float change = wanted - tempDelta;
        if (change != 0.0F) {
            values->ModActorValue(RE::ACTOR_VALUE_MODIFIER::kTemporary, kSpeed, change);
            tempDelta = wanted;
            // The movement code reads SpeedMult through a cached value that only the carry-weight
            // recomputation refreshes; a net-zero nudge triggers it without changing the capacity.
            values->ModActorValue(RE::ACTOR_VALUE_MODIFIER::kTemporary, RE::ActorValue::kCarryWeight, 1.0F);
            values->ModActorValue(RE::ACTOR_VALUE_MODIFIER::kTemporary, RE::ActorValue::kCarryWeight, -1.0F);
        }
        applied = value;
    }

    void Refresh()
    {
        const auto want = boost > 0.0F ? boost : manual;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        if (want <= 0.0F) {
            if (overridden) {
                Write(original);
                overridden = false;
            }
            return;
        }
        if (!overridden) {
            original = player->AsActorValueOwner()->GetBaseActorValue(kSpeed);
            overridden = true;
        }
        if (std::abs(applied - want) > 0.01F || !overridden) {
            Write(want);
        }
    }

    // The date globals are only recomputed by the engine at midnight, so a skip across midnights moves
    // day, month and year by hand to keep Game.time's calendar fields in step with daysPassed.
    void AdvanceDate(RE::Calendar& calendar, int daysToAdd)
    {
        auto day = static_cast<int>(calendar.gameDay->value) + daysToAdd;
        auto month = static_cast<int>(calendar.gameMonth->value);
        auto year = static_cast<int>(calendar.gameYear->value);
        while (month >= 0 && month < 12 && day > RE::Calendar::DAYS_IN_MONTH[month]) {
            day -= RE::Calendar::DAYS_IN_MONTH[month];
            if (++month == 12) {
                month = 0;
                ++year;
            }
        }
        calendar.gameDay->value = static_cast<float>(day);
        calendar.gameMonth->value = static_cast<float>(month);
        calendar.gameYear->value = static_cast<float>(year);
    }

    Json Describe()
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        Json out{ { "walkPercent", overridden ? Json(applied) : Json(nullptr) }, { "original", Round(original) },
            { "manual", manual > 0.0F ? Json(manual) : Json(nullptr) }, { "autoBoost", boost > 0.0F ? Json(boost) : Json(nullptr) } };
        if (player) {
            const auto values = player->AsActorValueOwner();
            out["speedMult"] = Round(values->GetActorValue(kSpeed));
            out["speedMultBase"] = Round(values->GetBaseActorValue(kSpeed));
        }
        if (observed.valid) {
            // Percent is what was requested; the measured speed is what the movement code delivered.
            out["measured"] = { { "unitsPerSecond", Round(observed.unitsPerSecond, 1) }, { "atPercent", Round(observed.percent) },
                { "perPercent", Round(observed.unitsPerSecond / std::max(observed.percent, 1.0F), 100.0) } };
        }
        if (const auto calendar = RE::Calendar::GetSingleton(); calendar && calendar->timeScale) {
            out["timescale"] = Round(calendar->timeScale->value);
        }
        return out;
    }
}

namespace devtools::speed
{
    Json SkipHours(float hours)
    {
        const auto calendar = RE::Calendar::GetSingleton();
        if (!calendar || !calendar->gameHour || !calendar->gameDaysPassed) {
            throw RpcError(code::notReady, "The calendar is not available yet");
        }
        const auto beforeHour = calendar->gameHour->value;
        const auto beforeDays = calendar->gameDaysPassed->value;
        const auto beforeRaw = calendar->rawDaysPassed;
        const auto beforeMidnights = calendar->midnightsPassed;
        // Every counter moves, since the engine rebuilds the global from them on each update and would otherwise
        // undo the skip or, with a wrongly converted counter, count part of it twice.
        const auto skipped = math::Skip({ beforeHour, beforeDays, beforeRaw, beforeMidnights }, hours);
        const auto crossed = skipped.crossed;
        const bool rawIsTotal = !skipped.rawExcludesToday;
        calendar->midnightsPassed = skipped.after.midnights;
        calendar->rawDaysPassed = static_cast<float>(skipped.after.raw);
        calendar->gameDaysPassed->value = static_cast<float>(skipped.after.daysPassed);
        calendar->gameHour->value = static_cast<float>(skipped.after.hour);
        if (calendar->gameDay && calendar->gameMonth && calendar->gameYear) {
            AdvanceDate(*calendar, static_cast<int>(crossed));
        }
        Json out{ { "waitedHours", hours }, { "hourBefore", Round(beforeHour, 2) }, { "hour", Round(calendar->gameHour->value, 2) },
            { "daysPassedBefore", Round(beforeDays, 3) }, { "daysPassed", Round(calendar->gameDaysPassed->value, 3) },
            { "rawDaysPassed", Round(calendar->rawDaysPassed, 3) }, { "rawIsTotal", rawIsTotal },
            { "midnightsPassed", calendar->midnightsPassed }, { "day", static_cast<int>(calendar->gameDay->value) },
            { "month", static_cast<int>(calendar->gameMonth->value) } };
        events::Emit("timeSkipped", out);
        return out;
    }

    void Observe(double unitsPerSecond)
    {
        observed = { unitsPerSecond, overridden ? applied : 100.0F, true };
    }

    float AppliedPercent() { return overridden ? applied : 100.0F; }

    void Boost(float percent)
    {
        boost = percent > 100.0F ? std::min(percent, maxWalk) : 0.0F;
        Refresh();
    }
}

DEVTOOLS_MODULE(Speed)
{
    // A loaded world carries its own saved value, so the override bookkeeping starts again.
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kPostLoadGame || message.type == SKSE::MessagingInterface::kNewGame) {
            overridden = false;
            manual = 0.0F;
            boost = 0.0F;
            applied = 0.0F;
            tempDelta = 0.0F;
            observed = {};
        }
    });

    Register("Game.setSpeed", [](const Json& params) {
        AllowOnly(params, { "walk", "timescale", "reset", "force", "loadEpoch" });
        RequirePlayable(params);
        if (Optional<bool>(params, "reset", false)) {
            manual = 0.0F;
            boost = 0.0F;
            Refresh();
        }
        if (params.contains("walk") && !params["walk"].is_null()) {
            const auto walk = Require<float>(params, "walk");
            const auto limit = Optional<bool>(params, "force", false) ? 1000.0F : speed::maxWalk;
            if (!(walk >= 25.0F && walk <= limit)) {
                throw RpcError(code::invalidParams, "walk is a speed multiplier in percent, 25 to " + std::to_string(static_cast<int>(limit)) + " (100 is normal)");
            }
            manual = walk == 100.0F ? 0.0F : walk;
            Refresh();
        }
        if (params.contains("timescale") && !params["timescale"].is_null()) {
            const auto value = Require<float>(params, "timescale");
            const auto calendar = RE::Calendar::GetSingleton();
            if (!(value >= 0.0F && value <= 10000.0F) || !calendar || !calendar->timeScale) {
                throw RpcError(code::invalidParams, "timescale is between 0 and 10000 (the default is 20)");
            }
            calendar->timeScale->value = value;
        }
        auto out = Describe();
        events::Emit("speedChange", out);
        return out;
    }, { .thread = Thread::Main });

    events::Declare("speedChange", "Walk speed or timescale changed through Game.setSpeed: {walkPercent, original, manual, autoBoost, speedMult, timescale}");

    Register("Game.speed", [](const Json& params) {
        AllowOnly(params, {});
        return Describe();
    }, { .thread = Thread::Main });

    Register("Game.waitHours", [](const Json& params) {
        AllowOnly(params, { "hours", "loadEpoch" });
        RequirePlayable(params);
        const auto hours = Require<float>(params, "hours");
        if (!(hours > 0.0F && hours <= 720.0F)) {
            throw RpcError(code::invalidParams, "hours is between 0 and 720");
        }
        return speed::SkipHours(hours);
    }, { .thread = Thread::Main });

    events::Declare("timeSkipped", "Game time was skipped with Game.waitHours or Game.fastTravel: {waitedHours, hourBefore, hour, ...}");
}
