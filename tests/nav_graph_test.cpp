// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the navmesh path search; built and run by test_nav_graph.py.
#include "../native/nav_graph.h"

#include <cstdio>
#include <cstdlib>

using namespace devtools::nav::graph;

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

    void Link(Graph& g, int a, int b)
    {
        for (int i = 0; i < 3; ++i) {
            for (int j = 0; j < 3; ++j) {
                const auto& p = g.verts[a];
                const auto& q = g.verts[b];
                const auto same = [](V3 u, V3 v) { return Len3(u - v) < 1e-6; };
                if (same(p[i], q[(j + 1) % 3]) && same(p[(i + 1) % 3], q[j])) {
                    g.nbr[a][i] = b;
                    g.nbr[b][j] = a;
                }
            }
        }
    }

    // An L-shaped corridor: east along y 0..100, then north along x 500..600.
    Graph Corridor()
    {
        Graph g;
        const V3 a{ 0, 0 }, b{ 500, 0 }, c{ 500, 100 }, d{ 0, 100 };
        const V3 e{ 600, 0 }, f{ 600, 100 }, h{ 600, 600 }, k{ 500, 600 };
        g.Add({ a, b, c });
        g.Add({ a, c, d });
        g.Add({ b, e, f });
        g.Add({ b, f, c });
        g.Add({ c, f, h });
        g.Add({ c, h, k });
        for (int x = 0; x < 6; ++x) {
            for (int y = x + 1; y < 6; ++y) {
                Link(g, x, y);
            }
        }
        return g;
    }
}

int main()
{
    const auto g = Corridor();
    const V3 start{ 50, 50 }, end{ 550, 550 };
    const int s = Locate(g, start, 500);
    const int t = Locate(g, end, 500);
    Check(s == 1 && t == 5, "locate");
    const auto search = AStar(g, s, t, end);
    Check(search.complete, "complete search");
    Check(search.tris.front() == s && search.tris.back() == t, "search ends");
    const auto path = Funnel(g, search.tris, start, end, 0.0);
    Check(path.size() == 3, "one bend at the inner corner");
    if (path.size() == 3) {
        Check(Len2(path[1] - V3{ 500, 100 }) < 1e-6, "bend is the corner");
    }
    const auto shrunk = Funnel(g, search.tris, start, end, 30.0);
    Check(shrunk.size() >= 3 && Len2(shrunk[1] - V3{ 500, 100 }) > 1.0, "shrink keeps off the corner");

    // Goal outside the graph: walk to the explored triangle nearest it.
    const auto partial = AStar(g, s, -1, V3{ 550, 5000 });
    Check(!partial.complete && partial.tris.back() == 5, "best partial");

    Check(SegmentDistance2D(V3{ 0, 10 }, V3{ -5, 0 }, V3{ 5, 0 }) == 10.0, "segment distance");
    Check(Locate(g, V3{ 5000, 5000 }, 100) == -1, "nothing near");
    if (failures == 0) {
        std::printf("ok\n");
    }
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
