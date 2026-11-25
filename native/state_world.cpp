// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// World queries: nearby references, the crosshair target, forms, globals and game time.
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <set>

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    const std::set<std::string> knownTypes{ "actor", "container", "door", "item", "activator", "furniture", "flora" };

    RE::TESGlobal* RequireGlobal(const Json& params)
    {
        const auto form = RequireForm(params);
        const auto global = form->As<RE::TESGlobal>();
        if (!global) {
            throw RpcError(code::invalidParams, "That form is not a global variable");
        }
        return global;
    }

    Json DescribeTime()
    {
        const auto calendar = RE::Calendar::GetSingleton();
        if (!calendar) {
            throw RpcError(code::notReady, "The calendar is not available yet");
        }
        char text[64]{};
        calendar->GetTimeDateString(text, sizeof(text), true);
        return Json{ { "hour", Round(calendar->GetHour(), 2) }, { "day", static_cast<int>(calendar->GetDay()) },
            { "month", calendar->GetMonthName() }, { "year", calendar->GetYear() }, { "dayName", calendar->GetDayName() },
            { "daysPassed", Round(calendar->GetDaysPassed(), 3) }, { "timescale", Round(calendar->GetTimescale()) },
            { "text", std::string(text) } };
    }
}

DEVTOOLS_MODULE(StateWorld)
{
    Register("World.nearby", [](const Json& params) {
        AllowOnly(params, { "radius", "types", "limit" });
        auto& player = RequirePlayer();
        const auto radius = std::clamp(Optional<float>(params, "radius", 1000.0f), 1.0f, 20000.0f);
        const auto limit = std::clamp(Optional<int>(params, "limit", 20), 1, 200);
        std::set<std::string> types;
        if (params.contains("types") && !params["types"].is_null()) {
            for (const auto& type : Require<std::vector<std::string>>(params, "types")) {
                if (!knownTypes.contains(type)) {
                    throw RpcError(code::invalidParams, "types are actor, container, door, item, activator, furniture, flora");
                }
                types.insert(type);
            }
        } else {
            types = { "actor", "container", "door", "item", "activator", "furniture" };
        }

        const auto origin = player.GetPosition();
        std::vector<std::pair<float, RE::TESObjectREFR*>> found;
        const auto tes = RE::TES::GetSingleton();
        if (!tes) {
            throw RpcError(code::notReady, "The world is not available yet");
        }
        tes->ForEachReferenceInRange(&player, radius, [&](RE::TESObjectREFR* ref) {
            if (ref && ref != &player && !ref->IsDisabled() && !ref->IsDeleted() && ref->GetBaseObject() && types.contains(RefCategory(ref))) {
                found.emplace_back(origin.GetDistance(ref->GetPosition()), ref);
            }
            return RE::BSContainer::ForEachResult::kContinue;
        });
        std::ranges::sort(found, [](const auto& a, const auto& b) { return a.first < b.first; });
        auto refs = Json::array();
        for (const auto& [distance, ref] : found) {
            if (static_cast<int>(refs.size()) >= limit) {
                break;
            }
            refs.push_back(DescribeRef(ref, &player));
        }
        return Json{ { "total", found.size() }, { "refs", std::move(refs) } };
    }, { .thread = Thread::Main });

    Register("World.crosshair", [](const Json& params) {
        AllowOnly(params, {});
        auto& player = RequirePlayer();
        const auto pick = RE::CrosshairPickData::GetSingleton();
        if (!pick) {
            throw RpcError(code::notReady, "Crosshair data is not available");
        }
        const auto target = RE::TESObjectREFR::LookupByHandle(pick->GetActiveTarget().native_handle());
        if (!target) {
            return Json{ { "ref", nullptr } };
        }
        auto out = DescribeRef(target.get(), &player);
        if (const auto base = target->GetBaseObject()) {
            RE::BSString text;
            if (base->GetActivateText(&player, text) && text.c_str() && *text.c_str()) {
                out["prompt"] = std::string(text.c_str());
            }
        }
        return Json{ { "ref", std::move(out) } };
    }, { .thread = Thread::Main });

    Register("Form.get", [](const Json& params) {
        AllowOnly(params, { "formID", "editorID" });
        return DescribeForm(RequireForm(params));
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });

    Register("Ref.get", [](const Json& params) {
        AllowOnly(params, { "refID" });
        const auto ref = RequireRef(params, "refID");
        auto out = DescribeRef(ref, RE::PlayerCharacter::GetSingleton());
        out["cell"] = DescribeCell(ref->GetParentCell());
        if (const auto owner = ref->GetOwner()) {
            out["owner"] = Json{ { "formID", Hex(owner->GetFormID()) }, { "name", Name(owner->GetName()) } };
        }
        out["loaded"] = ref->Is3DLoaded();
        return out;
    }, { .thread = Thread::Main });

    Register("Global.get", [](const Json& params) {
        AllowOnly(params, { "formID", "editorID" });
        const auto global = RequireGlobal(params);
        return Json{ { "formID", Hex(global->GetFormID()) }, { "editorID", Name(global->GetFormEditorID()) }, { "value", Round(global->value, 3) } };
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });

    Register("Game.time", [](const Json& params) {
        AllowOnly(params, {});
        return DescribeTime();
    }, { .thread = Thread::Main });

    Register("Game.setTimescale", [](const Json& params) {
        AllowOnly(params, { "value" });
        const auto value = Require<float>(params, "value");
        if (!(value >= 0.0f && value <= 10000.0f)) {
            throw RpcError(code::invalidParams, "value is between 0 and 10000 (the default is 20)");
        }
        const auto calendar = RE::Calendar::GetSingleton();
        if (!calendar || !calendar->timeScale) {
            throw RpcError(code::notReady, "The calendar is not available yet");
        }
        calendar->timeScale->value = value;
        return DescribeTime();
    }, { .thread = Thread::Main });
}
