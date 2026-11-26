// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Game-side glue for the stuck detector: option parsing, the pause check, the failure report
// and the events. The detection itself is in stuck.h.
#include "events.h"
#include "inspect.h"
#include "nav_common.h"

namespace devtools::nav::stuck
{
    namespace
    {
        void Take(const Json& spec, const char* key, double& out, double low, double high)
        {
            if (!spec.contains(key)) {
                return;
            }
            if (!spec[key].is_number()) {
                throw RpcError(code::invalidParams, std::string("stuck.") + key + " is a number");
            }
            const auto value = spec[key].get<double>();
            if (value < low || value > high) {
                throw RpcError(code::invalidParams, std::format("stuck.{} is {} to {}", key, low, high));
            }
            out = value;
        }
    }

    Config ParseConfig(const Json& params)
    {
        Config cfg;
        if (!params.contains("stuck")) {
            return cfg;
        }
        const auto& spec = params["stuck"];
        if (spec.is_boolean()) {
            cfg.enabled = spec.get<bool>();
            return cfg;
        }
        if (!spec.is_object()) {
            throw RpcError(code::invalidParams, "stuck is false or an object {distance, windowS, maxFailures, totalS, progressWindowS, progressGain, oscWindowS, oscRatio, doorRepeat, doorWindowS}");
        }
        AllowOnly(spec, { "enabled", "distance", "windowS", "maxFailures", "totalS", "progressWindowS", "progressGain",
            "oscWindowS", "oscRatio", "oscMinPath", "doorRepeat", "doorWindowS" });
        cfg.enabled = spec.value("enabled", true);
        double failures = cfg.maxFailures;
        double repeat = cfg.doorRepeat;
        Take(spec, "distance", cfg.distance, 1.0, 2000.0);
        Take(spec, "windowS", cfg.windowS, 0.5, 60.0);
        Take(spec, "maxFailures", failures, 1.0, 20.0);
        Take(spec, "totalS", cfg.totalS, 2.0, 600.0);
        Take(spec, "progressWindowS", cfg.progressWindowS, 3.0, 300.0);
        Take(spec, "progressGain", cfg.progressGain, 1.0, 5000.0);
        Take(spec, "oscWindowS", cfg.oscWindowS, 3.0, 120.0);
        Take(spec, "oscRatio", cfg.oscRatio, 0.01, 1.0);
        Take(spec, "oscMinPath", cfg.oscMinPath, 10.0, 10000.0);
        Take(spec, "doorRepeat", repeat, 2.0, 20.0);
        Take(spec, "doorWindowS", cfg.doorWindowS, 5.0, 3600.0);
        cfg.maxFailures = static_cast<int>(failures);
        cfg.doorRepeat = static_cast<int>(repeat);
        return cfg;
    }

    DoorLog& Doors()
    {
        static DoorLog log;
        return log;
    }

    bool Paused()
    {
        const auto ui = RE::UI::GetSingleton();
        if (!ui) {
            return true;
        }
        if (ui->GameIsPaused() || ui->IsMenuOpen("Loading Menu") || ui->IsMenuOpen("Main Menu") || !ui->IsMenuOpen("HUD Menu")) {
            return true;
        }
        return ui->IsMenuOpen(RE::DialogueMenu::MENU_NAME) || ui->IsMenuOpen(RE::MessageBoxMenu::MENU_NAME);
    }

    Json Report(const Verdict& verdict, RE::PlayerCharacter& player, const RE::NiPoint3* goal)
    {
        const auto pos = player.GetPosition();
        const auto heading = static_cast<double>(player.GetAngleZ());
        // The straight line ahead is what a wall-pusher is blocked on; a goal replaces it.
        const RE::NiPoint3 ahead{ pos.x + static_cast<float>(std::sin(heading) * 600.0), pos.y + static_cast<float>(std::cos(heading) * 600.0), pos.z };
        Json out{ { "reason", Name(verdict.reason) }, { "failures", verdict.failures }, { "stuckS", Round(verdict.episodeS) },
            { "pos", Point(pos) }, { "facingDeg", Round(Deg(heading), 10.0) }, { "blockers", inspect::Blockers(pos, goal ? *goal : ahead) },
            { "screenshotHint", "Call screenshot now: the view shows what the player is pushing against" } };
        if (verdict.door) {
            out["door"] = Hex(verdict.door);
        }
        return out;
    }

    void EmitStuck(const char* source, const Verdict& verdict, Json report)
    {
        report["source"] = source;
        report["step"] = Name(verdict.step);
        events::Emit("moveStuck", std::move(report));
    }

    void EmitUnstuck(const char* source, const Verdict& verdict, const RE::NiPoint3& pos)
    {
        events::Emit("moveUnstuck", { { "source", source }, { "failures", verdict.failures }, { "stuckS", Round(verdict.unstuckAfterS) },
            { "pos", Point(pos) } });
    }
}

DEVTOOLS_MODULE(Stuck)
{
    devtools::events::Declare("moveStuck", "Movement made no progress (reason stuck, noProgress, oscillation or doorLoop) and a recovery step starts or the command is stopped: {source: walk|nav, reason, step, failures, stuckS, pos, facingDeg, blockers, door?}");
    devtools::events::Declare("moveUnstuck", "The player got going again after moveStuck: {source, failures, stuckS, pos}");
}
