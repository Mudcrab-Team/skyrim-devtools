// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Walk speed multiplier, clock scale and skipping time. The SpeedMult actor value is 100 at normal
// speed; an automatic boost (Nav.goto fast) and a manual setting (Game.setSpeed) share one owner so
// the original value is restored exactly once.
#include "speed.h"

#include "events.h"
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
        // Both globals move together so day rollover and the day counter stay consistent; the engine
        // then derives the date from them on its next update.
        const double total = static_cast<double>(beforeDays) * 24.0 + beforeHour + hours;
        const double days = std::floor(total / 24.0);
        calendar->gameDaysPassed->value = static_cast<float>(days + (total - days * 24.0) / 24.0);
        calendar->gameHour->value = static_cast<float>(total - days * 24.0);
        Json out{ { "waitedHours", hours }, { "hourBefore", Round(beforeHour, 2) }, { "hour", Round(calendar->gameHour->value, 2) },
            { "daysPassedBefore", Round(beforeDays, 3) }, { "daysPassed", Round(calendar->gameDaysPassed->value, 3) } };
        events::Emit("timeSkipped", out);
        return out;
    }

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
