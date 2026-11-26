// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the long-range route search; built and run by test_nav_graph.py.
#include "../native/nav_global.h"

#include <cstdio>

using namespace devtools::nav;

namespace
{
    int failures = 0;

    void Check(bool ok, const char* what)
    {
        if (!ok) {
            std::printf("FAIL %s\n", what);
            ++failures;
        }
    }

    // Two ways from x=0 to x=4000: a straight one through node 1 that is expensive (rough
    // ground), and a longer road through nodes 2 and 3 that is cheap.
    constexpr const char* kToy =
        "skyrim-devtools-navgraph 1\n"
        "world Toy\n"
        "source Toy.esm\n"
        "nodes 5\n"
        "0 0 0\n2000 0 0\n1000 1000 0\n3000 1000 0\n4000 0 0\n"
        "edges 5\n"
        "0 1 5000\n1 4 5000\n0 2 1200\n2 3 1700\n3 4 1200\n";
}

int main()
{
    global::Graph g;
    Check(global::Parse(kToy, g).empty(), "parse");
    Check(g.world == "Toy" && g.nodes.size() == 5 && g.adj[0].size() == 2, "parsed shape");
    global::Graph bad;
    Check(!global::Parse("something else\n", bad).empty(), "rejects other files");
    Check(!global::Parse("skyrim-devtools-navgraph 1\nworld T\nsource\nnodes 1\n0 0 0\nedges 1\n0 7 1\n", bad).empty(), "rejects bad edge");

    const graph::V3 start{ -100, 0, 0 };
    const graph::V3 goal{ 4100, 0, 0 };
    auto route = global::Find(g, start, global::StraightLinks(g, start, 500.0, 1), goal, global::StraightLinks(g, goal, 500.0, 1));
    Check(route.found, "found");
    Check(route.points.size() == 6, "start, four gates, goal");
    Check(route.points.size() == 6 && route.points[2].y == 1000 && route.points[3].y == 1000, "takes the cheap road");
    Check(route.cost > 4100 && route.cost < 4500, "cost adds links and edges");

    // A known direct cost beats the graph when it is cheaper.
    auto direct = global::Find(g, start, global::StraightLinks(g, start, 500.0, 1), goal, global::StraightLinks(g, goal, 500.0, 1), 3000.0);
    Check(direct.found && direct.points.size() == 2, "direct link");

    // Nothing near the goal: no route.
    const graph::V3 far{ 90000, 0, 0 };
    Check(!global::Find(g, start, global::StraightLinks(g, start), far, global::StraightLinks(g, far)).found, "unreachable goal");

    // Following: points already passed are skipped, a detour off to the side is measured.
    const auto& pts = route.points;
    Check(global::Advance(pts, 1, { 1100, 950, 0 }) == 3, "skips passed points");
    Check(global::Advance(pts, 3, { 1100, 950, 0 }) == 3, "never goes back");
    Check(global::OffRoute(pts, 3, { 2000, 1000, 0 }) < 1.0, "on route");
    Check(global::OffRoute(pts, 3, { 2000, 4000, 0 }) > 2900.0, "off route");
    Check(std::abs(global::Remaining(pts, 5, { 4000, 0, 0 }) - 100.0) < 1e-6, "remaining at the end");
    return failures;
}
