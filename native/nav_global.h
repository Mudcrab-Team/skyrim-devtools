// SPDX-License-Identifier: GPL-3.0-or-later
// Long-range routes over a coarse graph of a whole worldspace, written offline by
// skyrim_devtools/navgraph.py from the user's own installed plugins. Nodes are gates (stretches of
// border where two cells' navmeshes join); edges are walking costs across one navmesh. The loaded
// navmeshes only reach a few cells, so Nav.goto plans the long way here and walks it a gate at a
// time with the local planner. Game-free so it can be tested off the PC.
#pragma once

#include "nav_graph.h"

#include <charconv>
#include <cmath>
#include <cstddef>
#include <limits>
#include <queue>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace devtools::nav::global
{
    using graph::V3;

    struct Graph
    {
        std::string world;
        std::vector<V3> nodes;
        std::vector<std::vector<std::pair<int, float>>> adj;
    };

    struct Link
    {
        int node = -1;
        double cost = 0.0;
    };

    struct Route
    {
        std::vector<V3> points;  // start, gates ..., goal
        bool found = false;
        double cost = 0.0;
        double length = 0.0;
        std::size_t expanded = 0;
        std::size_t startLinks = 0;
        std::size_t goalLinks = 0;
    };

    inline constexpr std::string_view kFormat = "skyrim-devtools-navgraph 1";
    // Straight lines to and from the graph cost this much more than their length: a guess at the
    // detour a straight line hides, so a far gate is not preferred for looking close.
    inline constexpr double kDetour = 1.3;
    // Gates and triangles favoured as roads cost 0.8 of their length, so A* stays admissible with
    // a heuristic scaled the same way.
    inline constexpr double kHeuristic = 0.8;

    namespace detail
    {
        struct Reader
        {
            std::string_view text;
            std::size_t pos = 0;

            std::string_view Line()
            {
                const auto end = text.find('\n', pos);
                auto line = text.substr(pos, end == std::string_view::npos ? std::string_view::npos : end - pos);
                pos = end == std::string_view::npos ? text.size() : end + 1;
                if (!line.empty() && line.back() == '\r') {
                    line.remove_suffix(1);
                }
                return line;
            }

            template <class T>
            bool Number(std::string_view& line, T& out)
            {
                while (!line.empty() && line.front() == ' ') {
                    line.remove_prefix(1);
                }
                const auto [end, ec] = std::from_chars(line.data(), line.data() + line.size(), out);
                if (ec != std::errc{}) {
                    return false;
                }
                line.remove_prefix(static_cast<std::size_t>(end - line.data()));
                return true;
            }
        };

        inline bool Count(Reader& r, std::string_view key, std::size_t& out)
        {
            auto line = r.Line();
            if (!line.starts_with(key)) {
                return false;
            }
            line.remove_prefix(key.size());
            return r.Number(line, out);
        }
    }

    // Parses a graph file's text; an empty error means success.
    inline std::string Parse(std::string_view text, Graph& out)
    {
        detail::Reader r{ text };
        if (r.Line() != kFormat) {
            return "not a navigation graph (expected the header \"" + std::string(kFormat) + "\")";
        }
        auto world = r.Line();
        if (!world.starts_with("world ")) {
            return "missing world line";
        }
        out.world = std::string(world.substr(6));
        r.Line();  // source plugins
        std::size_t nodes = 0;
        if (!detail::Count(r, "nodes ", nodes)) {
            return "missing node count";
        }
        out.nodes.resize(nodes);
        for (auto& node : out.nodes) {
            auto line = r.Line();
            if (!r.Number(line, node.x) || !r.Number(line, node.y) || !r.Number(line, node.z)) {
                return "bad node line";
            }
        }
        std::size_t edges = 0;
        if (!detail::Count(r, "edges ", edges)) {
            return "missing edge count";
        }
        out.adj.assign(nodes, {});
        for (std::size_t i = 0; i < edges; ++i) {
            auto line = r.Line();
            int a = 0;
            int b = 0;
            float cost = 0.0F;
            if (!r.Number(line, a) || !r.Number(line, b) || !r.Number(line, cost) || a < 0 || b < 0 ||
                static_cast<std::size_t>(a) >= nodes || static_cast<std::size_t>(b) >= nodes) {
                return "bad edge line";
            }
            out.adj[a].push_back({ b, cost });
            out.adj[b].push_back({ a, cost });
        }
        return {};
    }

    // Nodes within `reach` of p in plan view, nearest first, at most `limit`.
    inline std::vector<int> Near(const Graph& g, V3 p, double reach, std::size_t limit)
    {
        std::vector<std::pair<double, int>> found;
        for (int i = 0; i < static_cast<int>(g.nodes.size()); ++i) {
            const auto d = graph::Len2(g.nodes[i] - p);
            if (d <= reach) {
                found.push_back({ graph::Len3(g.nodes[i] - p), i });
            }
        }
        std::sort(found.begin(), found.end());
        std::vector<int> out;
        for (std::size_t k = 0; k < found.size() && k < limit; ++k) {
            out.push_back(found[k].second);
        }
        return out;
    }

    // Straight-line links from p to its nearest nodes, for when nothing better is known.
    inline std::vector<Link> StraightLinks(const Graph& g, V3 p, double reach = 6000.0, std::size_t limit = 12)
    {
        std::vector<Link> out;
        for (const auto i : Near(g, p, reach, limit)) {
            out.push_back({ i, graph::Len3(g.nodes[i] - p) * kDetour });
        }
        return out;
    }

    // A* from `start` (joined to the graph by startLinks) to `goal` (joined by goalLinks, the cost
    // from each node to the goal). `direct`, when finite, is a known cost straight to the goal.
    inline Route Find(const Graph& g, V3 start, const std::vector<Link>& startLinks, V3 goal, const std::vector<Link>& goalLinks,
        double direct = std::numeric_limits<double>::infinity())
    {
        Route out;
        out.startLinks = startLinks.size();
        out.goalLinks = goalLinks.size();
        const int n = static_cast<int>(g.nodes.size());
        const int startId = n;
        const int goalId = n + 1;
        std::vector<double> toGoal(n, std::numeric_limits<double>::infinity());
        for (const auto& link : goalLinks) {
            if (link.node >= 0 && link.node < n) {
                toGoal[link.node] = std::min(toGoal[link.node], link.cost);
            }
        }
        const auto pos = [&](int i) { return i == startId ? start : (i == goalId ? goal : g.nodes[i]); };
        const auto h = [&](int i) { return graph::Len3(pos(i) - goal) * kHeuristic; };
        std::vector<double> cost(n + 2, std::numeric_limits<double>::infinity());
        std::vector<int> parent(n + 2, -1);
        std::vector<char> closed(n + 2, 0);
        using Item = std::pair<double, int>;
        std::priority_queue<Item, std::vector<Item>, std::greater<>> open;
        const auto relax = [&](int from, int to, double step) {
            const auto next = cost[from] + step;
            if (next < cost[to]) {
                cost[to] = next;
                parent[to] = from;
                open.push({ next + h(to), to });
            }
        };
        cost[startId] = 0.0;
        open.push({ h(startId), startId });
        while (!open.empty()) {
            const auto [f, u] = open.top();
            open.pop();
            if (closed[u]) {
                continue;
            }
            closed[u] = 1;
            ++out.expanded;
            if (u == goalId) {
                break;
            }
            if (u == startId) {
                for (const auto& link : startLinks) {
                    if (link.node >= 0 && link.node < n) {
                        relax(u, link.node, link.cost);
                    }
                }
                if (std::isfinite(direct)) {
                    relax(u, goalId, direct);
                }
                continue;
            }
            for (const auto& [v, c] : g.adj[u]) {
                if (!closed[v]) {
                    relax(u, v, c);
                }
            }
            if (std::isfinite(toGoal[u])) {
                relax(u, goalId, toGoal[u]);
            }
        }
        if (!closed[goalId]) {
            return out;
        }
        std::vector<V3> reversed;
        for (int i = goalId; i >= 0; i = parent[i]) {
            reversed.push_back(pos(i));
        }
        out.points.assign(reversed.rbegin(), reversed.rend());
        out.found = true;
        out.cost = cost[goalId];
        for (std::size_t i = 1; i < out.points.size(); ++i) {
            out.length += graph::Len3(out.points[i] - out.points[i - 1]);
        }
        return out;
    }

    // Where along a route the player is: the index of the next route point to head for, given
    // the one it was heading for. Moves on past points the player has already passed (it is
    // nearer the following point than that point is), so a detour round a fight or a wrong turn
    // does not send it back. Looks at most `window` points ahead.
    inline std::size_t Advance(const std::vector<V3>& route, std::size_t next, V3 pos, std::size_t window = 6)
    {
        const auto last = std::min(route.size(), next + window);
        std::size_t best = next;
        for (auto i = next; i + 1 < last; ++i) {
            if (graph::Len2(route[i + 1] - pos) < graph::Len2(route[i + 1] - route[i])) {
                best = i + 1;
            }
        }
        return best;
    }

    // Plan-view distance from pos to the route between points from-1 and from+window.
    inline double OffRoute(const std::vector<V3>& route, std::size_t next, V3 pos, std::size_t window = 3)
    {
        if (route.empty()) {
            return std::numeric_limits<double>::infinity();
        }
        double best = graph::Len2(route[std::min(next, route.size() - 1)] - pos);
        const auto first = next > 0 ? next - 1 : 0;
        for (auto i = first; i + 1 < route.size() && i < next + window; ++i) {
            best = std::min(best, graph::SegmentDistance2D(pos, route[i], route[i + 1]));
        }
        return best;
    }

    // Length of the route from pos through point `next` to the end.
    inline double Remaining(const std::vector<V3>& route, std::size_t next, V3 pos)
    {
        if (next >= route.size()) {
            return route.empty() ? 0.0 : graph::Len3(route.back() - pos);
        }
        double total = graph::Len3(route[next] - pos);
        for (auto i = next + 1; i < route.size(); ++i) {
            total += graph::Len3(route[i] - route[i - 1]);
        }
        return total;
    }
}
