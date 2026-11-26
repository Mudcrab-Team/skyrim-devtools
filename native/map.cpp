// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// The world map: the player's custom waypoint and the map markers of locations. Nav.places lists
// where locations are; Map.markers adds what the player's map shows about them (discovered, fast
// travel), which decides whether a quest or travel test can use them.
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <type_traits>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    Json DescribeWorld(RE::TESWorldSpace* world)
    {
        if (!world) {
            return nullptr;
        }
        return Json{ { "formID", Hex(world->GetFormID()) }, { "editorID", Name(world->GetFormEditorID()) }, { "name", Name(world->GetName()) } };
    }

    RE::TESObjectREFR* Waypoint(RE::PlayerCharacter& player)
    {
        const auto marker = player.GetPlayerRuntimeData().playerMapMarker.get();
        if (!marker || marker->IsDisabled() || marker->IsDeleted()) {
            return nullptr;
        }
        return marker.get();
    }

    Json DescribeWaypoint(RE::PlayerCharacter& player)
    {
        const auto marker = Waypoint(player);
        if (!marker) {
            return Json{ { "set", false } };
        }
        const auto pos = marker->GetPosition();
        return Json{ { "set", true }, { "pos", Vec3(pos) }, { "refID", Hex(marker->GetFormID()) },
            { "world", DescribeWorld(marker->GetWorldspace()) }, { "distance", Round(player.GetPosition().GetDistance(pos)) } };
    }

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }
}

DEVTOOLS_MODULE(Map)
{
    Register("Map.getWaypoint", [](const Json& params) {
        AllowOnly(params, {});
        return DescribeWaypoint(RequirePlayer());
    }, { .thread = Thread::Main });

    // The engine places the marker where the map cursor is, so the map has to be open: the same path a
    // player's click takes, which keeps the marker's world and quest-compass behaviour intact.
    Register("Map.setWaypoint", [](const Json& params) {
        AllowOnly(params, { "x", "y", "z", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        auto& ui = RequireUI();
        const auto menu = ui.GetMenu<RE::MapMenu>();
        if (!menu || !ui.IsMenuOpen(RE::MapMenu::MENU_NAME)) {
            throw RpcError(code::notReady, "Open the map first (UI.openMenu with MapMenu), then wait a moment for it to load");
        }
        const RE::NiPoint3 target{ Require<float>(params, "x"), Require<float>(params, "y"), Optional<float>(params, "z", 0.0F) };
        // Some CommonLib pins return the runtime block by reference, others by pointer.
        auto&& runtime = menu->GetRuntimeData2();
        if constexpr (std::is_pointer_v<std::remove_reference_t<decltype(runtime)>>) {
            runtime->playerMarkerPosition = target;
        } else {
            runtime.playerMarkerPosition = target;
        }
        menu->PlaceMarker();
        auto out = DescribeWaypoint(player);
        out["requested"] = Vec3(target);
        return out;
    }, { .thread = Thread::Main });

    Register("Map.clearWaypoint", [](const Json& params) {
        AllowOnly(params, {});
        auto& player = RequirePlayer();
        const auto marker = Waypoint(player);
        const bool had = marker != nullptr;
        if (marker) {
            marker->Disable();
        }
        player.GetPlayerRuntimeData().playerMapMarker.reset();
        auto out = DescribeWaypoint(player);
        out["cleared"] = had;
        return out;
    }, { .thread = Thread::Main });

    Register("Map.markers", [](const Json& params) {
        AllowOnly(params, { "contains", "visible", "canTravel", "offset", "limit" });
        const auto contains = Lower(Optional<std::string>(params, "contains", ""));
        const auto offset = Optional<std::size_t>(params, "offset", 0);
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 50), std::size_t{ 1 }, std::size_t{ 500 });
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            throw RpcError(code::notReady, "game data is not loaded");
        }
        const auto player = RE::PlayerCharacter::GetSingleton();
        std::vector<Json> found;
        for (const auto location : data->GetFormArray<RE::BGSLocation>()) {
            const auto marker = location ? location->worldLocMarker.get() : nullptr;
            const auto full = location ? location->GetFullName() : nullptr;
            if (!marker || !full || !*full) {
                continue;
            }
            if (!contains.empty() && Lower(full).find(contains) == std::string::npos) {
                continue;
            }
            const auto extra = marker->extraList.GetByType<RE::ExtraMapMarker>();
            const auto map = extra ? extra->mapData : nullptr;
            const bool visible = map && map->flags.any(RE::MapMarkerData::Flag::kVisible);
            const bool travel = map && map->flags.any(RE::MapMarkerData::Flag::kCanTravelTo);
            if ((params.contains("visible") && Require<bool>(params, "visible") != visible) ||
                (params.contains("canTravel") && Require<bool>(params, "canTravel") != travel)) {
                continue;
            }
            Json entry{ { "name", full }, { "location", Hex(location->GetFormID()) }, { "marker", Hex(marker->GetFormID()) },
                { "pos", Vec3(marker->GetPosition()) }, { "world", DescribeWorld(marker->GetWorldspace()) },
                { "visible", visible }, { "canTravel", travel } };
            if (map) {
                entry["type"] = static_cast<int>(map->type.get());
            }
            if (player) {
                entry["distance"] = Round(player->GetPosition().GetDistance(marker->GetPosition()));
            }
            found.push_back(std::move(entry));
        }
        std::ranges::sort(found, [](const Json& a, const Json& b) { return a["name"].get<std::string>() < b["name"].get<std::string>(); });
        auto markers = Json::array();
        for (std::size_t i = offset; i < found.size() && markers.size() < limit; ++i) {
            markers.push_back(std::move(found[i]));
        }
        return Json{ { "total", found.size() }, { "offset", offset }, { "markers", std::move(markers) } };
    }, { .thread = Thread::Main, .timeout = 15s });
}
