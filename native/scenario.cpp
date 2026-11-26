// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Test setup primitives: character build (level, skills, perks, spells, items), bounty, weather,
// quest stages, and fast travel to discovered map markers.
#include "console.h"
#include "events.h"
#include "speed.h"
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <format>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    RE::ActorValue ParseAv(const std::string& name)
    {
        const auto wanted = Lower(name);
        for (int i = 0; i < static_cast<int>(RE::ActorValue::kTotal); ++i) {
            const auto info = RE::ActorValueList::GetActorValueInfo(static_cast<RE::ActorValue>(i));
            if (info && info->enumName && Lower(info->enumName) == wanted) {
                return static_cast<RE::ActorValue>(i);
            }
        }
        throw RpcError(code::invalidParams, "Unknown actor value '" + name + "'");
    }

    template <class T>
    T* ByName(const std::string& name)
    {
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            return nullptr;
        }
        const auto wanted = Lower(name);
        for (const auto form : data->GetFormArray<T>()) {
            const auto full = form ? form->GetName() : nullptr;
            if (full && *full && Lower(full) == wanted) {
                return form;
            }
        }
        return nullptr;
    }

    // A reference is a form id (hex string or integer) or a displayed name.
    template <class T>
    T* Resolve(const Json& spec)
    {
        if (spec.is_number_unsigned() || (spec.is_string() && spec.get<std::string>().find_first_not_of("0123456789abcdefABCDEFxX") == std::string::npos)) {
            return RE::TESForm::LookupByID<T>(ParseFormId(spec, "form"));
        }
        return ByName<T>(spec.get<std::string>());
    }

    RE::TESBoundObject* ResolveItem(const Json& spec)
    {
        if (spec.is_string() && spec.get<std::string>().find_first_not_of("0123456789abcdefABCDEFxX") != std::string::npos) {
            const auto name = spec.get<std::string>();
            if (const auto found = ByName<RE::TESObjectARMO>(name)) return found;
            if (const auto found = ByName<RE::TESObjectWEAP>(name)) return found;
            if (const auto found = ByName<RE::TESAmmo>(name)) return found;
            if (const auto found = ByName<RE::AlchemyItem>(name)) return found;
            if (const auto found = ByName<RE::ScrollItem>(name)) return found;
            if (const auto found = ByName<RE::TESObjectMISC>(name)) return found;
            if (const auto found = ByName<RE::TESObjectBOOK>(name)) return found;
            if (const auto found = ByName<RE::TESKey>(name)) return found;
            if (const auto found = ByName<RE::TESSoulGem>(name)) return found;
            if (const auto found = ByName<RE::IngredientItem>(name)) return found;
            return nullptr;
        }
        const auto form = RE::TESForm::LookupByID(ParseFormId(spec, "item"));
        return form ? form->As<RE::TESBoundObject>() : nullptr;
    }

    Json Step(const char* what, Json detail = Json::object())
    {
        detail["step"] = what;
        detail["ok"] = true;
        return detail;
    }

    Json Failed(const char* what, std::string reason, Json detail = Json::object())
    {
        detail["step"] = what;
        detail["ok"] = false;
        detail["reason"] = std::move(reason);
        return detail;
    }

    // Faction crime values are the bounty; clearing every crime-tracking faction empties all holds.
    int ClearBounty()
    {
        const auto data = RE::TESDataHandler::GetSingleton();
        int cleared = 0;
        if (!data) {
            return cleared;
        }
        for (const auto faction : data->GetFormArray<RE::TESFaction>()) {
            if (faction && faction->TracksCrimes() && (faction->GetCrimeGold() > 0 || faction->GetCrimeGoldViolent() > 0)) {
                faction->SetCrimeGold(0);
                faction->SetCrimeGoldViolent(0);
                ++cleared;
            }
        }
        return cleared;
    }

    RE::BGSLocation* FindLocation(const std::string& text)
    {
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            return nullptr;
        }
        const auto wanted = Lower(text);
        const bool isId = text.find_first_not_of("0123456789abcdefABCDEFxX") == std::string::npos;
        for (const auto location : data->GetFormArray<RE::BGSLocation>()) {
            if (!location || !location->worldLocMarker.get()) {
                continue;
            }
            const auto full = location->GetFullName();
            if (isId ? location->GetFormID() == ParseFormId(Json(text), "to") : (full && Lower(full) == wanted)) {
                return location;
            }
        }
        return nullptr;
    }

    RE::MapMarkerData* MarkerData(RE::TESObjectREFR* ref)
    {
        const auto extra = ref ? ref->extraList.GetByType<RE::ExtraMapMarker>() : nullptr;
        return extra ? extra->mapData : nullptr;
    }
}

DEVTOOLS_MODULE(Scenario)
{
    events::Declare("scenarioStep", "One scenario setup step finished: {step, ok, ...}");
    events::Declare("fastTravel", "The player was moved to a map marker: {to, marker, hours, distance}");

    // Character build. Every part is optional and reported on its own so one bad name does not hide the rest.
    Register("Scenario.character", [](const Json& params) {
        AllowOnly(params, { "level", "skills", "perks", "spells", "items", "clearBounty", "weather", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        auto steps = Json::array();
        const auto push = [&](Json step) {
            events::Emit("scenarioStep", step);
            steps.push_back(std::move(step));
        };

        if (params.contains("skills") && params["skills"].is_object()) {
            for (const auto& [name, value] : params["skills"].items()) {
                try {
                    const auto av = ParseAv(name);
                    const auto values = player.AsActorValueOwner();
                    const auto before = values->GetBaseActorValue(av);
                    values->SetActorValue(av, value.get<float>());
                    push(Step("skill", { { "av", name }, { "before", Round(before) }, { "after", Round(values->GetBaseActorValue(av)) } }));
                } catch (const std::exception& error) {
                    push(Failed("skill", error.what(), { { "av", name } }));
                }
            }
        }
        if (params.contains("perks")) {
            for (const auto& spec : params["perks"]) {
                const auto perk = Resolve<RE::BGSPerk>(spec);
                if (!perk) {
                    push(Failed("perk", "No such perk", { { "perk", spec } }));
                } else if (player.HasPerk(perk)) {
                    push(Step("perk", { { "perk", Name(perk->GetName()) }, { "already", true } }));
                } else {
                    player.AddPerk(perk);
                    push(Step("perk", { { "perk", Name(perk->GetName()) }, { "has", player.HasPerk(perk) } }));
                }
            }
        }
        if (params.contains("spells")) {
            for (const auto& spec : params["spells"]) {
                const auto spell = Resolve<RE::SpellItem>(spec);
                if (!spell) {
                    push(Failed("spell", "No such spell", { { "spell", spec } }));
                } else {
                    const bool added = player.AddSpell(spell);
                    push(Step("spell", { { "spell", Name(spell->GetName()) }, { "added", added } }));
                }
            }
        }
        if (params.contains("items")) {
            for (const auto& entry : params["items"]) {
                const auto& spec = entry.is_object() ? entry.at("item") : entry;
                const int count = entry.is_object() ? entry.value("count", 1) : 1;
                const bool equip = entry.is_object() && entry.value("equip", false);
                const auto object = ResolveItem(spec);
                if (!object || count < 1 || count > 1000000) {
                    push(Failed("item", object ? "count out of range" : "No such item", { { "item", spec } }));
                    continue;
                }
                player.AddObjectToContainer(object, nullptr, count, nullptr);
                Json detail{ { "item", Name(object->GetName()) }, { "formID", Hex(object->GetFormID()) }, { "count", count } };
                if (equip) {
                    if (const auto manager = RE::ActorEquipManager::GetSingleton()) {
                        manager->EquipObject(&player, object, nullptr, 1, nullptr, false, true, false, true);
                        detail["equipped"] = true;
                    }
                }
                push(Step("item", std::move(detail)));
            }
        }
        if (params.contains("level") && !params["level"].is_null()) {
            const auto level = Require<int>(params, "level");
            if (level < 1 || level > 81) {
                push(Failed("level", "level is 1 to 81"));
            } else {
                try {
                    const auto lines = console::Run("player.setlevel " + std::to_string(level), nullptr);
                    push(Step("level", { { "requested", level }, { "now", player.GetLevel() }, { "console", lines } }));
                } catch (const RpcError& error) {
                    push(Failed("level", error.message));
                }
            }
        }
        if (Optional<bool>(params, "clearBounty", false)) {
            push(Step("clearBounty", { { "factionsCleared", ClearBounty() } }));
        }
        if (params.contains("weather") && !params["weather"].is_null()) {
            const auto sky = RE::Sky::GetSingleton();
            const auto& spec = params["weather"];
            const auto weather = spec.is_string() && spec.get<std::string>().find_first_not_of("0123456789abcdefABCDEFxX") != std::string::npos
                                     ? [&]() -> RE::TESWeather* {
                                           const auto found = FindByEditorId(spec.get<std::string>());
                                           return found ? found->As<RE::TESWeather>() : nullptr;
                                       }()
                                     : Resolve<RE::TESWeather>(spec);
            if (!sky || !weather) {
                push(Failed("weather", "No such weather or no sky", { { "weather", params["weather"] } }));
            } else {
                sky->ForceWeather(weather, true);
                push(Step("weather", { { "weather", Hex(weather->GetFormID()) } }));
            }
        }
        return Json{ { "steps", std::move(steps) }, { "level", player.GetLevel() } };
    }, { .thread = Thread::Main, .timeout = 30s });

    // Quest stage through the engine's own console path, so stage fragments run as they do for a player.
    Register("Scenario.setStage", [](const Json& params) {
        AllowOnly(params, { "quest", "stage", "loadEpoch" });
        RequirePlayable(params);
        const auto name = Require<std::string>(params, "quest");
        const auto stage = Require<int>(params, "stage");
        if (stage < 0 || stage > 65535) {
            throw RpcError(code::invalidParams, "stage is 0 to 65535");
        }
        const auto form = name.find_first_not_of("0123456789abcdefABCDEFxX") == std::string::npos
                              ? RE::TESForm::LookupByID(ParseFormId(Json(name), "quest"))
                              : FindByEditorId(name);
        const auto quest = form ? form->As<RE::TESQuest>() : nullptr;
        if (!quest) {
            throw RpcError(code::notFound, "No such quest");
        }
        const auto before = quest->GetCurrentStageID();
        const bool wasRunning = quest->IsRunning() && quest->IsEnabled();
        const auto editor = quest->GetFormEditorID();
        if (!editor || !*editor) {
            throw RpcError(code::notFound, "The quest has no editor id the console can address");
        }
        auto lines = console::Run(std::string("startquest ") + editor, nullptr);
        auto more = console::Run(std::format("setstage {} {}", editor, stage), nullptr);
        lines.insert(lines.end(), more.begin(), more.end());
        return Json{ { "quest", editor }, { "stage", stage }, { "before", before }, { "after", quest->GetCurrentStageID() },
            { "wasRunning", wasRunning }, { "console", lines } };
    }, { .thread = Thread::Main, .timeout = 20s });

    // Teleport-style fast travel: the map UI is not driven, so the same rules are applied here. Only
    // markers the player discovered and can travel to are accepted unless forced.
    Register("Game.fastTravel", [](const Json& params) {
        AllowOnly(params, { "to", "force", "skipTime", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        const auto to = Require<std::string>(params, "to");
        RE::TESObjectREFR* marker = nullptr;
        std::string label;
        if (const auto location = FindLocation(to)) {
            marker = location->worldLocMarker.get().get();
            label = location->GetFullName();
        } else if (to.find_first_not_of("0123456789abcdefABCDEFxX") == std::string::npos) {
            const auto form = RE::TESForm::LookupByID(ParseFormId(Json(to), "to"));
            marker = form ? form->AsReference() : nullptr;
            label = to;
        }
        const auto data = MarkerData(marker);
        if (!marker || !data) {
            throw RpcError(code::notFound, "No map marker or location named '" + to + "'; see Nav.places");
        }
        const bool force = Optional<bool>(params, "force", false);
        const bool visible = data->flags.all(RE::MapMarkerData::Flag::kVisible);
        const bool canTravel = data->flags.all(RE::MapMarkerData::Flag::kCanTravelTo);
        if (!force && !(visible && canTravel)) {
            throw RpcError(code::notFound, "The map marker is not discovered; pass force to travel anyway",
                { { "visible", visible }, { "canTravelTo", canTravel }, { "to", label } });
        }
        const auto distance = player.GetPosition().GetDistance(marker->GetPosition());
        const auto from = player.GetParentCell() ? DescribeCell(player.GetParentCell()) : Json(nullptr);
        player.MoveTo(marker);
        Json out{ { "to", label }, { "marker", Hex(marker->GetFormID()) }, { "markerPos", Vec3(marker->GetPosition()) },
            { "distance", Round(distance, 0) }, { "forced", force && !(visible && canTravel) }, { "from", from } };
        // About 3 hours across 20000 units, in line with the game's own estimate; only when the worlds are the same.
        if (Optional<bool>(params, "skipTime", true) && distance > 500.0F) {
            const auto hours = std::clamp(distance / 6500.0F, 0.25F, 24.0F);
            out["time"] = speed::SkipHours(hours);
            out["hours"] = Round(hours, 2);
        }
        events::Emit("fastTravel", out);
        return out;
    }, { .thread = Thread::Main, .timeout = 20s });
}
