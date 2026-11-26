// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Loads the offline worldspace graphs and joins the player and the goal to them. The graph files
// are derived from the user's own plugins and live next to the runs, never in the repository:
// <scratch>/navgraph/<worldspace editor id>.navg, or the folder SKYRIM_DEVTOOLS_NAVGRAPH names.
#include "game.h"
#include "methods.h"
#include "nav_path.h"

#include <chrono>
#include <fstream>
#include <map>
#include <mutex>
#include <thread>
#include <memory>
#include <sstream>

namespace
{
    using namespace devtools;
    using namespace devtools::nav;

    struct Loaded
    {
        std::shared_ptr<const global::Graph> graph;
        std::filesystem::file_time_type stamp;
        std::string error;
    };

    // Guards the cache: a background read at start-up races the first Nav.goto.
    std::mutex cacheMutex;

    std::map<std::string, Loaded>& Cache()
    {
        static std::map<std::string, Loaded> cache;
        return cache;
    }

    std::string WorldName(RE::TESWorldSpace* world)
    {
        const auto id = world ? world->GetFormEditorID() : nullptr;
        return id && *id ? std::string(id) : std::string{};
    }

    std::filesystem::path GraphFile(const std::string& world)
    {
        wchar_t buffer[1024];
        const auto length = GetEnvironmentVariableW(L"SKYRIM_DEVTOOLS_NAVGRAPH", buffer, 1024);
        std::filesystem::path folder;
        if (length > 0 && length < 1024) {
            folder = std::filesystem::path(std::wstring(buffer, length));
        } else if (const auto run = RunDir(); !run.empty()) {
            folder = run.parent_path().parent_path() / "navgraph";  // <scratch>/runs/<session>
        }
        return folder.empty() ? std::filesystem::path{} : folder / (world + ".navg");
    }

    // The graph for a worldspace, read again when its file changes; null with a note when missing.
    std::shared_ptr<const global::Graph> Graph(const std::string& world, std::string& file, std::string& note)
    {
        const auto path = GraphFile(world);
        file = path.string();
        std::error_code ec;
        if (world.empty() || path.empty() || !std::filesystem::exists(path, ec)) {
            note = "No navigation graph for this worldspace; build one with skyrim-devtools-navgraph";
            return nullptr;
        }
        std::lock_guard lock(cacheMutex);
        const auto stamp = std::filesystem::last_write_time(path, ec);
        auto& entry = Cache()[world];
        if (entry.stamp != stamp || (!entry.graph && entry.error.empty())) {
            entry = {};
            entry.stamp = stamp;
            std::ifstream in(path, std::ios::binary);
            std::stringstream text;
            text << in.rdbuf();
            auto graph = std::make_shared<global::Graph>();
            entry.error = global::Parse(text.str(), *graph);
            if (entry.error.empty()) {
                entry.graph = std::move(graph);
            }
        }
        note = entry.error;
        return entry.graph;
    }

    graph::V3 V(const RE::NiPoint3& p) { return { p.x, p.y, p.z }; }
    RE::NiPoint3 P(const graph::V3& v) { return { static_cast<float>(v.x), static_cast<float>(v.y), static_cast<float>(v.z) }; }
}

namespace devtools::nav
{
    bool HasGlobalGraph(RE::TESWorldSpace* world)
    {
        std::string file;
        std::string note;
        return Graph(WorldName(world), file, note) != nullptr;
    }

    GlobalPlan PlanGlobal(RE::TESWorldSpace* world, const RE::NiPoint3& from, const RE::NiPoint3& to)
    {
        const auto started = std::chrono::steady_clock::now();
        GlobalPlan plan;
        plan.world = WorldName(world);
        const auto graph = Graph(plan.world, plan.file, plan.note);
        plan.available = graph != nullptr;
        if (graph) {
            // Join the start through the loaded navmeshes, so the first gate is one the player
            // can really walk to (not one across a river or up a cliff); a straight line only
            // when the player is off the loaded navmeshes.
            const auto nearby = global::Near(*graph, V(from), 9000.0, 48);
            std::vector<RE::NiPoint3> targets;
            for (const auto i : nearby) {
                targets.push_back(P(graph->nodes[i]));
            }
            targets.push_back(to);
            const auto costs = CostsTo(from, targets, 30000.0);
            std::vector<global::Link> startLinks;
            double direct = std::numeric_limits<double>::infinity();
            if (!costs.empty()) {
                for (std::size_t k = 0; k < nearby.size(); ++k) {
                    if (std::isfinite(costs[k])) {
                        startLinks.push_back({ nearby[k], costs[k] });
                    }
                }
                direct = costs.back();
            }
            if (startLinks.empty()) {
                startLinks = global::StraightLinks(*graph, V(from), 6000.0, 6);
            }
            plan.route = global::Find(*graph, V(from), startLinks, V(to), global::StraightLinks(*graph, V(to)), direct);
            if (!plan.route.found) {
                plan.note = startLinks.empty() ? "No gate of the graph near the start" : "No route over the graph to the goal";
            }
        }
        plan.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
        return plan;
    }

    Json GlobalJson(const GlobalPlan& plan, bool points)
    {
        Json out{ { "available", plan.available }, { "world", plan.world }, { "found", plan.route.found },
            { "length", Round(plan.route.length) }, { "cost", Round(plan.route.cost) }, { "nodes", plan.route.points.size() },
            { "expanded", plan.route.expanded }, { "startLinks", plan.route.startLinks }, { "ms", Round(plan.ms, 100.0) },
            { "note", plan.note.empty() ? Json(nullptr) : Json(plan.note) } };
        if (points) {
            Json list = Json::array();
            for (const auto& p : plan.route.points) {
                list.push_back(Point(P(p)));
            }
            out["points"] = std::move(list);
        }
        return out;
    }
}

// Reading and parsing a worldspace graph took up to half a second on the game thread at the first
// Nav.goto, which showed as a visible hitch; reading them once the data is loaded moves that off it.
DEVTOOLS_MODULE(NavGraphWarm)
{
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type != SKSE::MessagingInterface::kDataLoaded) {
            return;
        }
        std::thread([] {
            std::error_code ec;
            const auto folder = GraphFile("x").parent_path();
            if (folder.empty() || !std::filesystem::is_directory(folder, ec)) {
                return;
            }
            for (const auto& entry : std::filesystem::directory_iterator(folder, ec)) {
                if (entry.path().extension() == ".navg") {
                    std::string file;
                    std::string note;
                    Graph(entry.path().stem().string(), file, note);
                }
            }
        }).detach();
    });
}
