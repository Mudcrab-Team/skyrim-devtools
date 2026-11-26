// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// The engine keeps its own pathfinder behind types CommonLib leaves undefined, so input-mode
// navigation plans on a graph of its own: every triangle of every loaded navmesh, joined
// inside a mesh by neighbour indices and across meshes by edge portals.
#include "inspect.h"
#include "methods.h"
#include "nav_graph.h"
#include "nav_path.h"

#include <chrono>
#include <map>

namespace
{
    using namespace devtools;
    using namespace devtools::nav;
    namespace g = devtools::nav::graph;

    struct Cache
    {
        std::vector<RE::FormID> key;  // sorted ids of the meshes in the graph
        g::Graph graph;
        std::uint64_t generation = 0;
    };

    Cache& State()
    {
        static Cache cache;
        return cache;
    }

    g::V3 V(const RE::NiPoint3& p) { return { p.x, p.y, p.z }; }
    RE::NiPoint3 P(const g::V3& v) { return { static_cast<float>(v.x), static_cast<float>(v.y), static_cast<float>(v.z) }; }

    std::vector<RE::NavMesh*> LoadedMeshes()
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
        if (const auto tes = RE::TES::GetSingleton()) {
            tes->ForEachCell(add);
        }
        if (const auto player = RE::PlayerCharacter::GetSingleton()) {
            add(player->GetParentCell());
        }
        return meshes;
    }

    void Rebuild(const std::vector<RE::NavMesh*>& meshes, std::vector<RE::FormID> key)
    {
        using Flag = RE::BSNavmeshTriangle::TriangleFlag;
        auto& cache = State();
        g::Graph graph;
        std::map<RE::FormID, std::vector<int>> index;  // mesh id -> graph index per triangle
        for (const auto mesh : meshes) {
            auto& ids = index[mesh->GetFormID()];
            ids.assign(mesh->triangles.size(), -1);
            for (std::uint32_t t = 0; t < mesh->triangles.size(); ++t) {
                const auto& tri = mesh->triangles[t];
                if (tri.triangleFlags.underlying() & static_cast<std::uint16_t>(Flag::kDeleted)) {
                    continue;
                }
                std::array<g::V3, 3> v;
                bool valid = true;
                for (int i = 0; i < 3; ++i) {
                    if (tri.vertices[i] >= mesh->vertices.size()) {
                        valid = false;
                        break;
                    }
                    v[i] = V(mesh->vertices[tri.vertices[i]].location);
                }
                if (!valid) {
                    continue;
                }
                // Preferred triangles are what the game's own actors favour (roads, paths).
                const bool preferred = tri.triangleFlags.underlying() & static_cast<std::uint16_t>(Flag::kPreferred);
                ids[t] = graph.Add(v, preferred ? 0.8 : 1.0);
            }
        }
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
                            graph.nbr[from][i] = other->second[extra.portal.triangle];
                        }
                    } else if (link != 0xFFFF && link < ids.size()) {
                        graph.nbr[from][i] = ids[link];
                    }
                }
            }
        }
        graph.MarkWalls();
        cache.graph = std::move(graph);
        cache.key = std::move(key);
        ++cache.generation;
    }

    void Refresh()
    {
        const auto meshes = LoadedMeshes();
        std::vector<RE::FormID> key;
        key.reserve(meshes.size());
        for (const auto mesh : meshes) {
            key.push_back(mesh->GetFormID());
        }
        std::sort(key.begin(), key.end());
        if (key != State().key) {
            Rebuild(meshes, std::move(key));
        }
    }
}

namespace devtools::nav
{
    bool OnNavmesh(const RE::NiPoint3& p)
    {
        Refresh();
        return g::Locate(State().graph, V(p), 1.0) >= 0;
    }

    std::uint64_t GraphGeneration()
    {
        Refresh();
        return State().generation;
    }

    namespace
    {
        // The player's capsule is about 35 units wide each way; paths keep this far from wall corners
        // and treat anything under narrowPassage as a passage to enter straight on.
        constexpr double clearance = 55.0;
        constexpr double narrowPassage = 160.0;
    }

    Plan PlanPath(const RE::NiPoint3& from, const RE::NiPoint3& to)
    {
        const auto started = std::chrono::steady_clock::now();
        Refresh();
        const auto& graph = State().graph;
        Plan plan;
        plan.triangles = graph.Size();
        plan.meshes = State().key.size();
        const int start = g::Locate(graph, V(from), 400.0);
        const int goal = g::Locate(graph, V(to), 400.0);
        plan.goalOnMesh = goal >= 0;
        if (start < 0) {
            plan.note = "The start is not on a loaded navmesh";
        } else {
            const auto search = g::AStar(graph, start, goal, V(to));
            plan.found = !search.tris.empty();
            plan.complete = search.complete;
            plan.expanded = search.expanded;
            const auto end = search.complete ? V(to) : graph.Center(search.tris.back());
            for (const auto& p : g::Funnel(graph, search.tris, V(from), end, clearance, narrowPassage)) {
                plan.points.push_back(P(p));
            }
            if (!search.complete) {
                plan.note = goal < 0 ? "The goal is outside the loaded navmeshes; the path ends at the nearest reachable point"
                                     : "No route inside the loaded navmeshes; the path ends at the nearest reachable point";
            }
        }
        plan.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
        return plan;
    }

    std::vector<double> CostsTo(const RE::NiPoint3& from, const std::vector<RE::NiPoint3>& targets, double maxCost)
    {
        Refresh();
        const auto& graph = State().graph;
        const int start = g::Locate(graph, V(from), 400.0);
        if (start < 0) {
            return {};
        }
        const auto costs = g::Costs(graph, start, maxCost);
        std::vector<double> out;
        out.reserve(targets.size());
        for (const auto& target : targets) {
            const int t = g::Locate(graph, V(target), 150.0);
            out.push_back(t < 0 ? std::numeric_limits<double>::infinity()
                                : costs[t] + g::Len3(V(target) - graph.Center(t)) * graph.costScale[t]);
        }
        return out;
    }

    Json PlanJson(const Plan& plan)
    {
        Json points = Json::array();
        double length = 0.0;
        for (std::size_t i = 0; i < plan.points.size(); ++i) {
            points.push_back(Point(plan.points[i]));
            if (i > 0) {
                length += Dist3D(plan.points[i - 1], plan.points[i]);
            }
        }
        return { { "found", plan.found }, { "complete", plan.complete }, { "goalOnMesh", plan.goalOnMesh },
            { "points", std::move(points) }, { "length", Round(length) }, { "expanded", plan.expanded },
            { "triangles", plan.triangles }, { "meshes", plan.meshes }, { "ms", Round(plan.ms, 100.0) },
            { "note", plan.note.empty() ? Json(nullptr) : Json(plan.note) } };
    }
}

DEVTOOLS_MODULE(NavPath)
{
    Register("Nav.plan", [](const Json& params) {
        AllowOnly(params, { "refID", "x", "y", "z", "from", "route", "loadEpoch" });
        const auto route = Optional<std::string>(params, "route", "local");
        if (route != "local" && route != "global") {
            throw RpcError(code::invalidParams, "route is local or global");
        }
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        auto& player = Player();
        RE::NiPoint3 to;
        if (params.contains("refID")) {
            to = RequireRef(params["refID"])->GetPosition();
        } else {
            to = { Require<float>(params, "x"), Require<float>(params, "y"), Optional<float>(params, "z", player.GetPosition().z) };
        }
        RE::NiPoint3 from = player.GetPosition();
        if (params.contains("from")) {
            const auto& f = params["from"];
            if (!f.is_array() || f.size() != 3) {
                throw RpcError(code::invalidParams, "from is [x, y, z]");
            }
            from = { f[0].get<float>(), f[1].get<float>(), f[2].get<float>() };
        }
        if (route == "global") {
            // The long-range route over the worldspace graph, and the local plan to its first gate.
            const auto global = PlanGlobal(player.GetWorldspace(), from, to);
            auto out = PlanJson(PlanPath(from, global.route.points.size() > 1 ? P(global.route.points[1]) : to));
            out["global"] = GlobalJson(global);
            return out;
        }
        const auto plan = PlanPath(from, to);
        auto out = PlanJson(plan);
        if (plan.found && !plan.complete && !plan.points.empty()) {
            out["blockers"] = inspect::Blockers(plan.points.back(), to);
        }
        return out;
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(10) });
}
