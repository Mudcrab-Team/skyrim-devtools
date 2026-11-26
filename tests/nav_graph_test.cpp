// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the navmesh path search; built and run by test_nav_graph.py.
#include "../native/nav_graph.h"

#include <cmath>
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

namespace
{
    // Two rooms joined by a 90-unit gate at x 400..500, y 150..240.
    Graph Gate()
    {
        Graph g;
        const V3 a0{ 0, 0 }, a1{ 400, 150 }, a2{ 400, 240 }, a3{ 0, 400 };
        const V3 g0{ 400, 150 }, g1{ 500, 150 }, g2{ 500, 240 }, g3{ 400, 240 };
        const V3 b0{ 500, 150 }, b1{ 900, 0 }, b2{ 900, 400 }, b3{ 500, 240 };
        g.Add({ a0, a1, a2 });
        g.Add({ a0, a2, a3 });
        g.Add({ g0, g1, g2 });
        g.Add({ g0, g2, g3 });
        g.Add({ b0, b1, b2 });
        g.Add({ b0, b2, b3 });
        for (int x = 0; x < 6; ++x) {
            for (int y = x + 1; y < 6; ++y) {
                Link(g, x, y);
            }
        }
        g.MarkWalls();
        return g;
    }
}

int main()
{
    {
        // A deck above the ground: the actor's height picks the level, not the plan position.
        Graph g;
        g.Add({ V3{ 0, 0, 0 }, V3{ 1000, 0, 0 }, V3{ 0, 1000, 0 } });
        g.Add({ V3{ 0, 0, 220 }, V3{ 1000, 0, 220 }, V3{ 0, 1000, 220 } });
        Check(Locate(g, V3{ 200, 200, 10 }, 400) == 0, "under the deck resolves to the ground");
        Check(Locate(g, V3{ 200, 200, 215 }, 400) == 1, "on the deck resolves to the deck");
        Check(Locate(g, V3{ 200, 200, 200 }, 400) == 1, "a goal near deck height resolves to the deck");
        Graph ramp;
        ramp.Add({ V3{ 0, 0, 0 }, V3{ 1000, 0, 400 }, V3{ 0, 1000, 0 } });
        Check(std::abs(PlaneZ(ramp.verts[0], V3{ 500, 0, 0 }) - 200.0) < 1e-6, "plane height on a ramp");
    }
    {
        const auto gate = Gate();
        const V3 start{ 50, 380 }, end{ 850, 380 };
        const auto search = AStar(gate, Locate(gate, start, 500), Locate(gate, end, 500), end);
        Check(search.complete, "gate search");
        const auto bare = Funnel(gate, search.tris, start, end, 0.0);
        const auto kept = Funnel(gate, search.tris, start, end, 55.0, 160.0, 80.0);
        bool hugs = false;
        for (const auto& p : bare) {
            hugs = hugs || (p.x > 399 && p.x < 501 && (std::abs(p.y - 150) < 20 || std::abs(p.y - 240) < 20));
        }
        Check(hugs, "without clearance the path touches the gate frame");
        bool through = false;
        bool straight = false;
        for (std::size_t i = 0; i < kept.size(); ++i) {
            through = through || (std::abs(kept[i].x - 400) < 1.0 && std::abs(kept[i].y - 195) < 1.0);
            straight = straight || (kept[i].x < 400 && std::abs(kept[i].y - 195) < 1.0 && kept[i].x > 300);
        }
        Check(through, "path goes through the middle of the gate");
        Check(straight, "lead-in point before the gate on its centreline");
    }
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
