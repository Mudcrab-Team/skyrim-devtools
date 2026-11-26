// SPDX-License-Identifier: GPL-3.0-or-later
// Game-independent path search over a triangle graph: A* with a best-partial fallback and a
// funnel (string-pulling) pass. Kept free of game types so it can be tested off the PC.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <limits>
#include <queue>
#include <unordered_set>
#include <utility>
#include <vector>

namespace devtools::nav::graph
{
    struct V3
    {
        double x = 0.0;
        double y = 0.0;
        double z = 0.0;
    };

    inline V3 operator-(V3 a, V3 b) { return { a.x - b.x, a.y - b.y, a.z - b.z }; }
    inline V3 operator+(V3 a, V3 b) { return { a.x + b.x, a.y + b.y, a.z + b.z }; }
    inline V3 operator*(V3 a, double s) { return { a.x * s, a.y * s, a.z * s }; }
    inline double Cross2(V3 a, V3 b) { return a.x * b.y - a.y * b.x; }
    inline double Len2(V3 a) { return std::hypot(a.x, a.y); }
    inline double Len3(V3 a) { return std::sqrt(a.x * a.x + a.y * a.y + a.z * a.z); }

    // Triangle t's edge i runs from verts[t][i] to verts[t][(i + 1) % 3]; nbr[t][i] is the
    // triangle across it, or -1.
    struct Graph
    {
        std::vector<std::array<V3, 3>> verts;
        std::vector<std::array<int, 3>> nbr;
        std::vector<double> costScale;  // 1 normally; below 1 for preferred triangles
        // Vertices that touch a boundary edge (a wall or the edge of the loaded meshes), by 1-unit grid
        // key; empty until MarkWalls runs, in which case every vertex counts as a wall.
        std::unordered_set<std::int64_t> walls;
        bool wallsKnown = false;

        static std::int64_t Key(V3 p)
        {
            return (static_cast<std::int64_t>(std::llround(p.x)) << 32) ^ (static_cast<std::int64_t>(std::llround(p.y)) & 0xFFFFFFFFLL);
        }
        void MarkWalls()
        {
            walls.clear();
            for (std::size_t t = 0; t < verts.size(); ++t) {
                for (int i = 0; i < 3; ++i) {
                    if (nbr[t][i] < 0) {
                        walls.insert(Key(verts[t][i]));
                        walls.insert(Key(verts[t][(i + 1) % 3]));
                    }
                }
            }
            wallsKnown = true;
        }
        [[nodiscard]] bool IsWall(V3 p) const { return !wallsKnown || walls.contains(Key(p)); }

        [[nodiscard]] std::size_t Size() const { return verts.size(); }
        [[nodiscard]] V3 Center(int t) const
        {
            const auto& v = verts[t];
            return (v[0] + v[1] + v[2]) * (1.0 / 3.0);
        }
        [[nodiscard]] V3 EdgeMid(int t, int i) const { return (verts[t][i] + verts[t][(i + 1) % 3]) * 0.5; }
        int Add(std::array<V3, 3> v, double scale = 1.0)
        {
            verts.push_back(v);
            nbr.push_back({ -1, -1, -1 });
            costScale.push_back(scale);
            return static_cast<int>(verts.size()) - 1;
        }
    };

    inline bool InTriangle2D(const std::array<V3, 3>& v, V3 p)
    {
        const auto d0 = Cross2(v[1] - v[0], p - v[0]);
        const auto d1 = Cross2(v[2] - v[1], p - v[1]);
        const auto d2 = Cross2(v[0] - v[2], p - v[2]);
        const bool neg = d0 < 0 || d1 < 0 || d2 < 0;
        const bool pos = d0 > 0 || d1 > 0 || d2 > 0;
        return !(neg && pos);
    }

    // Height of triangle t's plane at p's x, y (the centroid height for a degenerate triangle).
    inline double PlaneZ(const std::array<V3, 3>& v, V3 p)
    {
        const auto e1 = v[1] - v[0];
        const auto e2 = v[2] - v[0];
        const double det = e1.x * e2.y - e1.y * e2.x;
        if (std::abs(det) < 1e-9) {
            return (v[0].z + v[1].z + v[2].z) / 3.0;
        }
        const double dx = p.x - v[0].x, dy = p.y - v[0].y;
        const double a = (dx * e2.y - dy * e2.x) / det;
        const double b = (e1.x * dy - e1.y * dx) / det;
        return v[0].z + a * e1.z + b * e2.z;
    }

    // The triangle under p: among those containing p in plan view, the one whose plane is nearest
    // p's height (a bridge deck or the ground below it, whichever the actor is on), else the
    // triangle with the nearest centre in 3D within maxDist. -1 when nothing is close.
    inline int Locate(const Graph& g, V3 p, double maxDist, double maxStep = 160.0)
    {
        int best = -1;
        double bestDz = std::numeric_limits<double>::max();
        int nearest = -1;
        double nearestD = maxDist;
        for (int t = 0; t < static_cast<int>(g.Size()); ++t) {
            const auto& v = g.verts[t];
            if (InTriangle2D(v, p)) {
                const auto dz = std::abs(PlaneZ(v, p) - p.z);
                if (dz < bestDz) {
                    bestDz = dz;
                    best = t;
                }
            }
            const auto d = Len3(g.Center(t) - p);
            if (d < nearestD) {
                nearestD = d;
                nearest = t;
            }
        }
        return best >= 0 && bestDz <= maxStep ? best : nearest;
    }

    struct Search
    {
        std::vector<int> tris;  // start ... end
        bool complete = false;  // reached the goal triangle
        std::size_t expanded = 0;
        double cost = 0.0;
    };

    // A* from start to goal (or towards goalPos when goal is -1, e.g. outside the loaded
    // area). Without a route the result is the expanded triangle nearest goalPos, so the caller
    // can walk there and plan again once more of the world has streamed in.
    inline Search AStar(const Graph& g, int start, int goal, V3 goalPos, std::size_t maxExpand = 200000)
    {
        Search out;
        if (start < 0 || start >= static_cast<int>(g.Size())) {
            return out;
        }
        const auto n = g.Size();
        std::vector<double> cost(n, std::numeric_limits<double>::infinity());
        std::vector<int> parent(n, -1);
        std::vector<char> closed(n, 0);
        using Item = std::pair<double, int>;
        std::priority_queue<Item, std::vector<Item>, std::greater<>> open;
        cost[start] = 0.0;
        open.push({ Len3(g.Center(start) - goalPos), start });
        int best = start;
        double bestH = Len3(g.Center(start) - goalPos);
        int reached = -1;
        while (!open.empty() && out.expanded < maxExpand) {
            const auto [f, t] = open.top();
            open.pop();
            if (closed[t]) {
                continue;
            }
            closed[t] = 1;
            ++out.expanded;
            if (t == goal) {
                reached = t;
                break;
            }
            const auto h = Len3(g.Center(t) - goalPos);
            if (h < bestH) {
                bestH = h;
                best = t;
            }
            for (int i = 0; i < 3; ++i) {
                const int u = g.nbr[t][i];
                if (u < 0 || closed[u]) {
                    continue;
                }
                // Through the shared edge's midpoint, so long thin triangles don't skew costs.
                const auto mid = g.EdgeMid(t, i);
                const auto step = (Len3(mid - g.Center(t)) * g.costScale[t] + Len3(g.Center(u) - mid) * g.costScale[u]);
                const auto next = cost[t] + step;
                if (next < cost[u]) {
                    cost[u] = next;
                    parent[u] = t;
                    open.push({ next + Len3(g.Center(u) - goalPos), u });
                }
            }
        }
        const int end = reached >= 0 ? reached : best;
        out.complete = reached >= 0;
        out.cost = cost[end];
        for (int t = end; t >= 0; t = parent[t]) {
            out.tris.push_back(t);
        }
        std::reverse(out.tris.begin(), out.tris.end());
        return out;
    }

    // Walking cost from `start` to every triangle within maxCost (infinity elsewhere), with the
    // same step costs as AStar; for joining a point to many destinations at once.
    inline std::vector<double> Costs(const Graph& g, int start, double maxCost)
    {
        std::vector<double> cost(g.Size(), std::numeric_limits<double>::infinity());
        if (start < 0 || start >= static_cast<int>(g.Size())) {
            return cost;
        }
        using Item = std::pair<double, int>;
        std::priority_queue<Item, std::vector<Item>, std::greater<>> open;
        cost[start] = 0.0;
        open.push({ 0.0, start });
        while (!open.empty()) {
            const auto [c, t] = open.top();
            open.pop();
            if (c > cost[t] || c > maxCost) {
                continue;
            }
            for (int i = 0; i < 3; ++i) {
                const int u = g.nbr[t][i];
                if (u < 0) {
                    continue;
                }
                const auto mid = g.EdgeMid(t, i);
                const auto next = c + Len3(mid - g.Center(t)) * g.costScale[t] + Len3(g.Center(u) - mid) * g.costScale[u];
                if (next < cost[u]) {
                    cost[u] = next;
                    open.push({ next, u });
                }
            }
        }
        return cost;
    }

    // The shared edge from triangle a to its neighbour b as (left, right) seen when walking
    // from a into b. Ends that touch a wall are pulled inwards by `shrink` (the actor's radius plus a
    // margin) so the path keeps clear of corners; when that would leave nothing (a passage narrower
    // than twice the clearance) both ends become the midpoint. `width` reports the open width.
    inline bool Portal(const Graph& g, int a, int b, double shrink, V3& left, V3& right, double* width = nullptr, bool* bothWalls = nullptr)
    {
        for (int i = 0; i < 3; ++i) {
            if (g.nbr[a][i] != b) {
                continue;
            }
            V3 p = g.verts[a][i];
            V3 q = g.verts[a][(i + 1) % 3];
            const auto dir = g.Center(b) - g.Center(a);
            if (Cross2(dir, p - q) > 0) {
                left = p;
                right = q;
            } else {
                left = q;
                right = p;
            }
            const auto span = Len2(right - left);
            if (width) {
                *width = span;
            }
            if (span > 1e-6) {
                const double pullLeft = g.IsWall(left) ? shrink : 0.0;
                const double pullRight = g.IsWall(right) ? shrink : 0.0;
                if (bothWalls) {
                    *bothWalls = pullLeft > 0.0 && pullRight > 0.0;
                }
                const auto along = right - left;
                if (pullLeft + pullRight > span * 0.9) {
                    const auto mid = left + along * 0.5;
                    left = right = mid;
                } else {
                    left = left + along * (pullLeft / span);
                    right = right - along * (pullRight / span);
                }
            }
            return true;
        }
        return false;
    }

    // Simple stupid funnel: the shortest polyline from start to end through the corridor of
    // triangles, in plan view; heights come from the portal points it bends at.
    // Passages narrower than `narrow` get a lead-in point `lead` units before and after their middle,
    // on the line through it perpendicular to the opening, so they are entered straight.
    inline std::vector<V3> Funnel(const Graph& g, const std::vector<int>& tris, V3 start, V3 end, double shrink = 30.0,
                                  double narrow = 0.0, double lead = 80.0)
    {
        std::vector<std::pair<V3, V3>> portals;
        portals.push_back({ start, start });
        for (std::size_t k = 0; k + 1 < tris.size(); ++k) {
            V3 l, r;
            double width = 0.0;
            bool walled = false;
            if (Portal(g, tris[k], tris[k + 1], shrink, l, r, &width, &walled)) {
                if (narrow > 0.0 && width < narrow && walled) {
                    // Perpendicular to the opening, pointing the way the corridor goes.
                    const V3 mid = (l + r) * 0.5;
                    const auto ahead = g.Center(tris[k + 1]) - g.Center(tris[k]);
                    V3 n{ 0, 0, 0 };
                    for (int i = 0; i < 3; ++i) {
                        if (g.nbr[tris[k]][i] == tris[k + 1]) {
                            const auto edge = g.verts[tris[k]][(i + 1) % 3] - g.verts[tris[k]][i];
                            n = V3{ -edge.y, edge.x, 0 };
                        }
                    }
                    const auto len = Len2(n);
                    if (len > 1e-6) {
                        n = n * (1.0 / len);
                        if (n.x * ahead.x + n.y * ahead.y < 0) {
                            n = n * -1.0;
                        }
                        const V3 before = mid - n * lead;
                        const V3 after = mid + n * lead;
                        // On the corridor itself, a triangle or two either side of the opening.
                        const auto inside = [&](std::size_t from, std::size_t to, V3 p) {
                            for (auto j = from; j <= to && j < tris.size(); ++j) {
                                if (InTriangle2D(g.verts[tris[j]], p)) {
                                    return true;
                                }
                            }
                            return false;
                        };
                        const bool beforeOk = inside(k >= 2 ? k - 2 : 0, k, before);
                        const bool afterOk = inside(k + 1, k + 3, after);
                        if (beforeOk) {
                            portals.push_back({ before, before });
                        }
                        portals.push_back({ mid, mid });
                        if (afterOk) {
                            portals.push_back({ after, after });
                        }
                        continue;
                    }
                }
                portals.push_back({ l, r });
            }
        }
        portals.push_back({ end, end });

        std::vector<V3> path{ start };
        V3 apex = start;
        V3 left = start;
        V3 right = start;
        std::size_t apexIndex = 0, leftIndex = 0, rightIndex = 0;
        auto same = [](V3 a, V3 b) { return Len2(a - b) < 1e-3; };
        for (std::size_t i = 1; i < portals.size(); ++i) {
            const auto [newLeft, newRight] = portals[i];
            // Right side: tighten while the new point is not clockwise of the right edge.
            if (Cross2(right - apex, newRight - apex) >= 0) {
                if (same(apex, right) || Cross2(left - apex, newRight - apex) < 0) {
                    right = newRight;
                    rightIndex = i;
                } else {
                    if (!same(path.back(), left)) {
                        path.push_back(left);
                    }
                    apex = left;
                    apexIndex = leftIndex;
                    left = right = apex;
                    leftIndex = rightIndex = apexIndex;
                    i = apexIndex;
                    continue;
                }
            }
            // Left side, mirrored.
            if (Cross2(left - apex, newLeft - apex) <= 0) {
                if (same(apex, left) || Cross2(right - apex, newLeft - apex) > 0) {
                    left = newLeft;
                    leftIndex = i;
                } else {
                    if (!same(path.back(), right)) {
                        path.push_back(right);
                    }
                    apex = right;
                    apexIndex = rightIndex;
                    left = right = apex;
                    leftIndex = rightIndex = apexIndex;
                    i = apexIndex;
                    continue;
                }
            }
        }
        if (!same(path.back(), end)) {
            path.push_back(end);
        }
        return path;
    }

    // Distance from p to segment ab in plan view.
    inline double SegmentDistance2D(V3 p, V3 a, V3 b)
    {
        const auto ab = b - a;
        const auto len2 = ab.x * ab.x + ab.y * ab.y;
        double t = len2 > 0 ? ((p.x - a.x) * ab.x + (p.y - a.y) * ab.y) / len2 : 0.0;
        t = std::clamp(t, 0.0, 1.0);
        return Len2(p - (a + ab * t));
    }
}
