// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Read-only raw material for the client's cell map (skyrim_devtools/rooms.py): the navmesh triangles
// of the player's cell, what lies in the cell, and the places the player has stood in it. The planner
// is untouched; this module only reads the same engine structures.
#include "methods.h"
#include "nav_common.h"
#include "state_common.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <set>

namespace
{
    using namespace devtools;
    namespace v = devtools::view;

    constexpr double kVisitBinXY = 90.0;
    constexpr double kVisitBinZ = 160.0;
    constexpr double kSampleEveryS = 0.25;
    constexpr std::size_t kMaxVisitPoints = 6000;

    // Meshes of the player's cell; in an exterior, of every loaded cell.
    std::vector<RE::NavMesh*> CellMeshes(RE::PlayerCharacter& player, bool interior)
    {
        std::vector<RE::NavMesh*> meshes;
        auto add = [&](RE::TESObjectCELL* cell) {
            if (!cell) {
                return;
            }
            const auto array = cell->GetRuntimeData().navMeshes;
            if (!array) {
                return;
            }
            for (auto& mesh : array->navMeshes) {
                if (mesh && std::find(meshes.begin(), meshes.end(), mesh.get()) == meshes.end()) {
                    meshes.push_back(mesh.get());
                }
            }
        };
        if (!interior) {
            if (const auto tes = RE::TES::GetSingleton()) {
                tes->ForEachCell(add);
            }
        }
        add(player.GetParentCell());
        return meshes;
    }

    Json NavMeshJson(RE::PlayerCharacter& player, double radius, std::size_t maxTriangles)
    {
        using Flag = RE::BSNavmeshTriangle::TriangleFlag;
        const auto cell = player.GetParentCell();
        const bool interior = cell && cell->IsInteriorCell();
        const auto origin = player.GetPosition();
        const auto meshes = CellMeshes(player, interior);

        std::vector<int> verts;
        std::vector<int> tris;
        std::vector<int> nbr;
        std::map<RE::FormID, std::vector<int>> index;
        bool truncated = false;
        for (const auto mesh : meshes) {
            auto& ids = index[mesh->GetFormID()];
            ids.assign(mesh->triangles.size(), -1);
            std::map<std::uint32_t, int> remap;
            for (std::uint32_t t = 0; t < mesh->triangles.size(); ++t) {
                const auto& tri = mesh->triangles[t];
                if (tri.triangleFlags.underlying() & static_cast<std::uint16_t>(Flag::kDeleted)) {
                    continue;
                }
                bool valid = true;
                RE::NiPoint3 centre{ 0.0F, 0.0F, 0.0F };
                for (int i = 0; i < 3; ++i) {
                    if (tri.vertices[i] >= mesh->vertices.size()) {
                        valid = false;
                        break;
                    }
                    centre += mesh->vertices[tri.vertices[i]].location * (1.0F / 3.0F);
                }
                if (!valid || (!interior && nav::Dist2D(centre, origin) > radius)) {
                    continue;
                }
                if (tris.size() / 3 >= maxTriangles) {
                    truncated = true;
                    continue;
                }
                ids[t] = static_cast<int>(tris.size() / 3);
                for (int i = 0; i < 3; ++i) {
                    const auto vertex = tri.vertices[i];
                    auto found = remap.find(vertex);
                    if (found == remap.end()) {
                        const auto& p = mesh->vertices[vertex].location;
                        found = remap.emplace(vertex, static_cast<int>(verts.size() / 3)).first;
                        verts.push_back(static_cast<int>(std::lround(p.x)));
                        verts.push_back(static_cast<int>(std::lround(p.y)));
                        verts.push_back(static_cast<int>(std::lround(p.z)));
                    }
                    tris.push_back(found->second);
                }
            }
        }
        nbr.assign(tris.size(), -1);
        for (const auto mesh : meshes) {
            const auto& ids = index[mesh->GetFormID()];
            for (std::uint32_t t = 0; t < mesh->triangles.size(); ++t) {
                const int from = ids[t];
                if (from < 0) {
                    continue;
                }
                const auto& tri = mesh->triangles[t];
                for (int i = 0; i < 3; ++i) {
                    const auto link = tri.triangles[i];
                    int to = -1;
                    if (tri.triangleFlags.underlying() & (1u << i)) {
                        if (link >= mesh->extraEdgeInfo.size()) {
                            continue;
                        }
                        const auto& extra = mesh->extraEdgeInfo[link];
                        if (extra.type.get() != RE::EDGE_EXTRA_INFO_TYPE::kPortal) {
                            continue;
                        }
                        const auto other = index.find(extra.portal.otherMeshID);
                        if (other != index.end() && extra.portal.triangle < other->second.size()) {
                            to = other->second[extra.portal.triangle];
                        }
                    } else if (link != 0xFFFF && link < ids.size()) {
                        to = ids[link];
                    }
                    nbr[static_cast<std::size_t>(from) * 3 + i] = to;
                }
            }
        }
        return { { "cell", v::DescribeCell(cell) }, { "interior", interior }, { "origin", v::Vec3(origin) }, { "meshes", meshes.size() },
            { "triangles", tris.size() / 3 }, { "truncated", truncated }, { "v", std::move(verts) }, { "t", std::move(tris) }, { "n", std::move(nbr) } };
    }

    double MaxDimension(RE::TESBoundObject* base)
    {
        const auto& b = base->boundData;
        return std::max({ std::abs(b.boundMax.x - b.boundMin.x), std::abs(b.boundMax.y - b.boundMin.y), std::abs(b.boundMax.z - b.boundMin.z) });
    }

    bool NotableItem(RE::TESBoundObject* base)
    {
        switch (base->GetFormType()) {
        case RE::FormType::KeyMaster:
        case RE::FormType::Book:
        case RE::FormType::Note:
        case RE::FormType::Scroll:
        case RE::FormType::SoulGem:
        case RE::FormType::Weapon:
        case RE::FormType::Armor:
            return true;
        default:
            return base->GetGoldValue() >= 40;
        }
    }

    Json ContentsJson(RE::PlayerCharacter& player, double radius)
    {
        const auto cell = player.GetParentCell();
        const bool interior = cell && cell->IsInteriorCell();
        Json actors = Json::array(), containers = Json::array(), doors = Json::array(), activators = Json::array(), items = Json::array();
        std::size_t dead = 0, skipped = 0;
        const auto visit = [&](RE::TESObjectREFR* ref) {
            if (!ref || ref == &player || ref->IsDisabled() || ref->IsDeleted() || !ref->GetBaseObject()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto category = v::RefCategory(ref);
            if (category == "actor") {
                const auto actor = ref->As<RE::Actor>();
                if (actor->IsDead()) {
                    ++dead;
                    return RE::BSContainer::ForEachResult::kContinue;
                }
                auto entry = v::DescribeRef(ref, &player);
                if (const auto base = actor->GetActorBase()) {
                    entry["unique"] = base->IsUnique();
                }
                entry["essential"] = actor->IsEssential();
                if (const auto target = actor->GetActorRuntimeData().currentCombatTarget.get().get(); target == &player) {
                    entry["hostile"] = true;
                }
                actors.push_back(std::move(entry));
            } else if (category == "container") {
                auto entry = v::DescribeRef(ref, &player);
                entry["locked"] = ref->IsLocked();
                entry["size"] = v::Round(MaxDimension(ref->GetBaseObject()));
                if (const auto container = ref->GetBaseObject()->As<RE::TESObjectCONT>()) {
                    entry["entries"] = container->numContainerObjects;
                }
                containers.push_back(std::move(entry));
            } else if (category == "door") {
                doors.push_back(v::DescribeRef(ref, &player));
            } else if (category == "activator") {
                const auto name = v::RefName(ref);
                if (name && *name) {
                    activators.push_back(v::DescribeRef(ref, &player));
                }
            } else if (category == "item") {
                if (NotableItem(ref->GetBaseObject()) && v::RefName(ref) && *v::RefName(ref)) {
                    auto entry = v::DescribeRef(ref, &player);
                    entry["value"] = ref->GetBaseObject()->GetGoldValue();
                    items.push_back(std::move(entry));
                } else {
                    ++skipped;
                }
            }
            return RE::BSContainer::ForEachResult::kContinue;
        };
        if (interior) {
            cell->ForEachReference(visit);
        } else if (const auto tes = RE::TES::GetSingleton()) {
            tes->ForEachReferenceInRange(&player, static_cast<float>(radius), visit);
        }
        return { { "cell", v::DescribeCell(cell) }, { "interior", interior }, { "player", v::Vec3(player.GetPosition()) },
            { "godMode", RE::PlayerCharacter::IsGodMode() }, { "actors", std::move(actors) }, { "deadActors", dead }, { "containers", std::move(containers) }, { "doors", std::move(doors) },
            { "activators", std::move(activators) }, { "items", std::move(items) }, { "minorItemsSkipped", skipped } };
    }

    // Where the player has been, per cell, binned so a long stay stays small.
    struct CellVisits
    {
        std::set<std::array<int, 3>> bins;
        std::vector<std::array<float, 3>> points;
        std::size_t samples = 0;
        std::string name;
    };

    std::map<RE::FormID, CellVisits>& Visits()
    {
        static std::map<RE::FormID, CellVisits> visits;
        return visits;
    }

    void SampleVisit(nav::Tick& tick)
    {
        static double last = 0.0;
        if (tick.now - last < kSampleEveryS) {
            return;
        }
        last = tick.now;
        const auto ui = RE::UI::GetSingleton();
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!ui || !player || !Playable(*ui)) {
            return;
        }
        const auto cell = player->GetParentCell();
        if (!cell) {
            return;
        }
        auto& visits = Visits()[cell->GetFormID()];
        if (visits.name.empty() && cell->GetName()) {
            visits.name = cell->GetName();
        }
        const auto p = player->GetPosition();
        ++visits.samples;
        const std::array<int, 3> bin{ static_cast<int>(std::floor(p.x / kVisitBinXY)), static_cast<int>(std::floor(p.y / kVisitBinXY)),
            static_cast<int>(std::floor(p.z / kVisitBinZ)) };
        if (visits.bins.insert(bin).second && visits.points.size() < kMaxVisitPoints) {
            visits.points.push_back({ p.x, p.y, p.z });
        }
    }
}

DEVTOOLS_MODULE(CellMap)
{
    nav::AddTicker(SampleVisit);

    Register("Cell.navmesh", [](const Json& params) {
        AllowOnly(params, { "radius", "maxTriangles" });
        auto& player = v::RequirePlayer();
        const auto radius = std::clamp(Optional<double>(params, "radius", 6000.0), 500.0, 30000.0);
        const auto limit = static_cast<std::size_t>(std::clamp(Optional<int>(params, "maxTriangles", 30000), 100, 100000));
        return NavMeshJson(player, radius, limit);
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(20) });

    Register("Cell.contents", [](const Json& params) {
        AllowOnly(params, { "radius" });
        auto& player = v::RequirePlayer();
        return ContentsJson(player, std::clamp(Optional<double>(params, "radius", 6000.0), 500.0, 30000.0));
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(10) });

    Register("Cell.visited", [](const Json& params) {
        AllowOnly(params, { "reset" });
        auto& player = v::RequirePlayer();
        std::lock_guard lock(nav::Lock());
        const auto cell = player.GetParentCell();
        if (!cell) {
            throw RpcError(code::notReady, "The player is in no cell");
        }
        auto& visits = Visits()[cell->GetFormID()];
        if (Optional<bool>(params, "reset", false)) {
            visits = {};
            visits.name = cell->GetName() ? cell->GetName() : "";
        }
        Json points = Json::array();
        for (const auto& p : visits.points) {
            points.push_back(Json::array({ std::lround(p[0]), std::lround(p[1]), std::lround(p[2]) }));
        }
        return Json{ { "cell", v::DescribeCell(cell) }, { "samples", visits.samples }, { "points", std::move(points) },
            { "binXY", kVisitBinXY }, { "binZ", kVisitBinZ }, { "cellsTracked", Visits().size() } };
    }, { .thread = Thread::Main });
}
