"""Long-range navigation graph from the user's own installed game plugins (SPDX-License-Identifier: GPL-3.0-or-later).

The plugin's input-mode navigation only sees the navmeshes of the loaded cells, and the engine's own
long-range pathfinder reports no path from some places. This builds a coarse graph of a whole
worldspace offline, Baritone style: the long route is planned here, and the plugin walks it in short
segments with its local planner.

  skyrim-devtools-navgraph build --data "<game>/Data" --world Tamriel -o Tamriel.navg
  skyrim-devtools-navgraph route Tamriel.navg --from X,Y,Z --to X,Y,Z
  skyrim-devtools-navgraph probe --data "<game>/Data"

Nodes are gates: stretches of border where the navmeshes of two neighbouring cells are joined by edge
portals, cut into pieces of at most GATE_SPAN units. Edges join gates of the same navmesh, weighted by
the shortest path between them over that mesh's triangles, with the same costs the plugin's local
planner uses (preferred triangles, which are roads and paths, are cheaper). Gates sit on navmesh edges,
so every route point lies on a walkable triangle.

The graph is derived from game data and is meant to stay on the machine that has the game; only this
generator is part of the repository. It needs nothing beyond the standard library, so it runs with any
Python 3.10+ next to the game.
"""

from __future__ import annotations

import argparse
import heapq
import math
import mmap
import os
from pathlib import Path
import struct
import sys
import time
import zlib

FORMAT = "skyrim-devtools-navgraph 1"
GATE_SPAN = 1200.0       # longest stretch of border one gate stands for
GATE_GAP = 400.0         # a wider gap between portal edges starts a new gate
PREFERRED_SCALE = 0.8    # same as the plugin's local planner
BASE_MASTERS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]

HEADER = struct.Struct("<4sIIIIHH")  # type, size, flags, formID, vcs, version, unknown (records)
GROUP = struct.Struct("<4sI4siHHI")  # GRUP, size incl. header, label, type, stamp, vc, unknown
SUB = struct.Struct("<4sH")
FLAG_COMPRESSED = 0x00040000
FLAG_DELETED = 0x00000020
TRI_DELETED = 0x0008
TRI_PREFERRED = 0x0040
EDGE_PORTAL = 0


# ---- Plugin files ----

class Plugin:
    """One plugin file: its masters and the records of the wanted types in WRLD groups."""

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        with open(self.path, "rb") as handle:
            self.data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        kind, size, flags, _, _, _, _ = HEADER.unpack_from(self.data, 0)
        if kind != b"TES4":
            raise ValueError(f"{self.name} is not a plugin (no TES4 header)")
        self.masters = [value.split(b"\0", 1)[0].decode("utf-8", "replace")
                        for sub, value in subrecords(self.payload(0, size, flags)) if sub == b"MAST"]
        self.start = HEADER.size + size

    def payload(self, offset, size, flags):
        raw = self.data[offset + HEADER.size: offset + HEADER.size + size]
        if flags & FLAG_COMPRESSED:
            return zlib.decompress(raw[4:])
        return raw

    def origin(self, form_id):
        """The file a form id was first defined in, and the id without its load-order byte."""
        index = form_id >> 24
        name = self.masters[index] if index < len(self.masters) else self.name
        return name.lower(), form_id & 0xFFFFFF

    def records(self, types, top=(b"WRLD",)):
        """(type, flags, formID, payload) of every record of the given types inside the top groups."""
        pos, end = self.start, len(self.data)
        while pos + GROUP.size <= end:
            tag, size, label, _, _, _, _ = GROUP.unpack_from(self.data, pos)
            if tag != b"GRUP":
                raise ValueError(f"{self.name}: expected a group at {pos}")
            if label in top:
                yield from self._walk(pos + GROUP.size, pos + size, types)
            pos += size

    def _walk(self, pos, end, types):
        data = self.data
        while pos < end:
            tag, size, flags, form_id, _, _, _ = HEADER.unpack_from(data, pos)
            if tag == b"GRUP":
                pos += GROUP.size  # nested groups lie inside this one: step in, not over
                continue
            if tag in types:
                yield tag, flags, form_id, (b"" if flags & FLAG_DELETED else self.payload(pos, size, flags))
            pos += HEADER.size + size


def subrecords(payload):
    pos, end, big = 0, len(payload), None
    while pos + SUB.size <= end:
        tag, size = SUB.unpack_from(payload, pos)
        pos += SUB.size
        if big is not None:
            size, big = big, None
        if tag == b"XXXX":
            big = struct.unpack_from("<I", payload, pos)[0]
            pos += size
            continue
        yield tag, payload[pos:pos + size]
        pos += size


def load_order(data_dir, plugins=None):
    """The plugin files to read, in load order: the given list, else the base masters and the
    Creation Club files the game lists in Skyrim.ccc (or every cc*.esm/esl present)."""
    data_dir = Path(data_dir)
    if plugins:
        names = list(plugins)
    else:
        names = [n for n in BASE_MASTERS if (data_dir / n).exists()]
        ccc = data_dir.parent / "Skyrim.ccc"
        if ccc.exists():
            extra = [line.strip() for line in ccc.read_text(errors="replace").splitlines() if line.strip()]
        else:
            extra = sorted(p.name for p in data_dir.iterdir() if p.name.lower().startswith("cc")
                           and p.suffix.lower() in {".esm", ".esl"})
        names += [n for n in extra if (data_dir / n).exists() and n not in names]
    return [data_dir / n for n in names]


# ---- Navmeshes ----

class Mesh:
    __slots__ = ("key", "world", "grid", "verts", "tris", "links")

    def __init__(self, key, world, grid, verts, tris, links):
        self.key = key        # (origin file, id)
        self.world = world    # (origin file, id) of the parent worldspace, or None for interiors
        self.grid = grid      # (x, y) cell of an exterior mesh
        self.verts = verts    # [(x, y, z)]
        self.tris = tris      # [(v0, v1, v2, e0, e1, e2, flags)]
        self.links = links    # [(type, mesh key, triangle)]


def parse_nvnm(blob, plugin, link_size=None):
    """Vertices, triangles and edge links of one NVNM subrecord; the rest (door triangles, cover,
    search grid) is not needed. Edge links are 10 bytes on disk in some versions and 11 (with the
    edge index) in others, so both are tried and checked against the rest of the record."""
    off = 0
    _version, _magic, world = struct.unpack_from("<III", blob, off)
    off += 12
    grid = None
    if world:
        gy, gx = struct.unpack_from("<hh", blob, off)
        grid = (gx, gy)
    off += 4
    (nv,) = struct.unpack_from("<I", blob, off)
    off += 4
    flat = struct.unpack_from(f"<{nv * 3}f", blob, off)
    verts = [flat[i:i + 3] for i in range(0, nv * 3, 3)]
    off += nv * 12
    (nt,) = struct.unpack_from("<I", blob, off)
    off += 4
    tris = []
    for v0, v1, v2, e0, e1, e2, flags, _cover in struct.iter_unpack("<HHHhhhHH", blob[off:off + nt * 16]):
        tris.append((v0, v1, v2, e0, e1, e2, flags))
    off += nt * 16
    (ne,) = struct.unpack_from("<I", blob, off)
    off += 4
    sizes = [link_size] if link_size else [10, 11]
    for size in sizes:
        if link_size or _links_fit(blob, off + ne * size, len(blob)):
            links = []
            for k in range(ne):
                kind, mesh, tri = struct.unpack_from("<IIH", blob, off + k * size)
                links.append((kind, plugin.origin(mesh) if mesh else None, tri))
            return (plugin.origin(world) if world else None), grid, verts, tris, links, size
    raise ValueError("unrecognised NVNM edge-link layout")


def _links_fit(blob, off, end):
    """Whether door triangles, cover triangles and the search grid follow at `off` and end the record."""
    try:
        (nd,) = struct.unpack_from("<I", blob, off)
        off += 4 + nd * 10
        (nc,) = struct.unpack_from("<I", blob, off)
        off += 4 + nc * 2
        (divisor,) = struct.unpack_from("<I", blob, off)
        off += 4 + 8 + 24
        if divisor > 4096:
            return False
        for _ in range(divisor * divisor):
            (n,) = struct.unpack_from("<I", blob, off)
            off += 4 + n * 2
            if off > end:
                return False
        return off == end
    except struct.error:
        return False


def read_world_meshes(paths, log=print):
    """Every navmesh and worldspace editor id after load-order overrides: ({mesh key: Mesh}, {editor id: world key})."""
    meshes, worlds, link_size = {}, {}, None
    for path in paths:
        plugin = Plugin(path)
        started = time.monotonic()
        count = 0
        for tag, flags, form_id, payload in plugin.records({b"NAVM", b"WRLD"}):
            key = plugin.origin(form_id)
            if tag == b"WRLD":
                for sub, value in subrecords(payload):
                    if sub == b"EDID":
                        worlds[value.split(b"\0", 1)[0].decode("utf-8", "replace")] = key
                continue
            if flags & FLAG_DELETED:
                meshes.pop(key, None)
                continue
            for sub, value in subrecords(payload):
                if sub == b"NVNM":
                    world, grid, verts, tris, links, link_size = parse_nvnm(value, plugin, link_size)
                    meshes[key] = Mesh(key, world, grid, verts, tris, links)
                    count += 1
        log(f"{plugin.name}: {count} navmeshes in {time.monotonic() - started:.1f}s")
    return meshes, worlds


# ---- Graph ----

def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _center(mesh, t):
    v0, v1, v2 = mesh.tris[t][:3]
    a, b, c = mesh.verts[v0], mesh.verts[v1], mesh.verts[v2]
    return ((a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3)


def _valid(mesh, t):
    if t < 0 or t >= len(mesh.tris):
        return False
    tri = mesh.tris[t]
    nv = len(mesh.verts)
    return not tri[6] & TRI_DELETED and tri[0] < nv and tri[1] < nv and tri[2] < nv


def crossings(meshes):
    """Portal edges between two meshes of the set, once per physical edge:
    {(mesh a, mesh b): [(midpoint, triangle in a, triangle in b)]} with a < b."""
    seen = set()
    pairs = {}
    for a, mesh in meshes.items():
        for t, tri in enumerate(mesh.tris):
            if not _valid(mesh, t):
                continue
            for i in range(3):
                if not tri[6] & (1 << i):
                    continue
                link = tri[3 + i]
                if link < 0 or link >= len(mesh.links):
                    continue
                kind, b, other = mesh.links[link]
                if kind != EDGE_PORTAL or b not in meshes or b == a or not _valid(meshes[b], other):
                    continue
                p = mesh.verts[tri[i]]
                q = mesh.verts[tri[(i + 1) % 3]]
                mid = _mid(p, q)
                lo, hi = (a, b) if a < b else (b, a)
                key = (lo, hi, round(mid[0] / 8), round(mid[1] / 8), round(mid[2] / 32))
                if key in seen:
                    continue
                seen.add(key)
                pairs.setdefault((lo, hi), []).append((mid, t, other) if a < b else (mid, other, t))
    return pairs


def gates(pairs):
    """Cuts each border into gates: [(position, (mesh a, tri a), (mesh b, tri b))]."""
    out = []
    for (a, b), edges in pairs.items():
        xs = [e[0][0] for e in edges]
        ys = [e[0][1] for e in edges]
        axis = 0 if max(xs) - min(xs) >= max(ys) - min(ys) else 1
        edges = sorted(edges, key=lambda e: e[0][axis])
        chunk = []

        def close():
            if chunk:
                cx = sum(e[0][0] for e in chunk) / len(chunk)
                cy = sum(e[0][1] for e in chunk) / len(chunk)
                rep = min(chunk, key=lambda e: (e[0][0] - cx) ** 2 + (e[0][1] - cy) ** 2)
                out.append((rep[0], (a, rep[1]), (b, rep[2])))

        for edge in edges:
            if chunk and (_dist(edge[0], chunk[-1][0]) > GATE_GAP or _dist(edge[0], chunk[0][0]) > GATE_SPAN):
                close()
                chunk = []
            chunk.append(edge)
        close()
    return out


def _mesh_graph(mesh):
    """Centres, cost scales and internal neighbours of one mesh's triangles."""
    n = len(mesh.tris)
    centers = [None] * n
    scale = [1.0] * n
    nbr = [()] * n
    for t, tri in enumerate(mesh.tris):
        if not _valid(mesh, t):
            continue
        centers[t] = _center(mesh, t)
        scale[t] = PREFERRED_SCALE if tri[6] & TRI_PREFERRED else 1.0
        links = []
        for i in range(3):
            if tri[6] & (1 << i):
                continue
            u = tri[3 + i]
            if 0 <= u < n and _valid(mesh, u):
                links.append((u, _mid(mesh.verts[tri[i]], mesh.verts[tri[(i + 1) % 3]])))
        nbr[t] = tuple(links)
    return centers, scale, nbr


def _dijkstra(centers, scale, nbr, source, start_cost, targets):
    """Costs from `source` to every triangle in `targets` through edge midpoints, like the plugin."""
    best = {source: start_cost}
    heap = [(start_cost, source)]
    left = set(targets)
    done = set()
    while heap and left:
        cost, t = heapq.heappop(heap)
        if t in done:
            continue
        done.add(t)
        left.discard(t)
        ct = centers[t]
        for u, mid in nbr[t]:
            if u in done:
                continue
            step = _dist(ct, mid) * scale[t] + _dist(mid, centers[u]) * scale[u]
            nxt = cost + step
            if nxt < best.get(u, math.inf):
                best[u] = nxt
                heapq.heappush(heap, (nxt, u))
    return best


def build_graph(meshes, world, log=print):
    """(nodes [(x, y, z)], edges [(a, b, cost)]) for the navmeshes of one worldspace."""
    started = time.monotonic()
    chosen = {k: m for k, m in meshes.items() if m.world == world}
    gate_list = gates(crossings(chosen))
    log(f"{len(chosen)} navmeshes, {len(gate_list)} gates ({time.monotonic() - started:.1f}s)")
    per_mesh = {}
    for index, (pos, side_a, side_b) in enumerate(gate_list):
        for mesh, tri in (side_a, side_b):
            per_mesh.setdefault(mesh, []).append((index, tri, pos))
    edges = {}
    for done, (key, members) in enumerate(per_mesh.items()):
        if len(members) < 2:
            continue
        mesh = chosen[key]
        centers, scale, nbr = _mesh_graph(mesh)
        tris = {tri for _, tri, _ in members}
        for k, (g, tri, pos) in enumerate(members[:-1]):
            costs = _dijkstra(centers, scale, nbr, tri, _dist(pos, centers[tri]) * scale[tri], tris)
            for h, other, there in members[k + 1:]:
                if h == g:
                    continue
                if other == tri:
                    cost = _dist(pos, there) * scale[tri]
                elif other in costs:
                    cost = costs[other] + _dist(centers[other], there) * scale[other]
                else:
                    continue
                pair = (g, h) if g < h else (h, g)
                if cost < edges.get(pair, math.inf):
                    edges[pair] = cost
        if done and done % 2000 == 0:
            log(f"  {done}/{len(per_mesh)} meshes, {len(edges)} edges ({time.monotonic() - started:.1f}s)")
    nodes = [g[0] for g in gate_list]
    log(f"{len(nodes)} nodes, {len(edges)} edges ({time.monotonic() - started:.1f}s)")
    return nodes, [(a, b, c) for (a, b), c in sorted(edges.items())]


def write_graph(path, world_name, nodes, edges, sources=()):
    lines = [FORMAT, f"world {world_name}", "source " + ",".join(sources), f"nodes {len(nodes)}"]
    lines += [f"{x:.1f} {y:.1f} {z:.1f}" for x, y, z in nodes]
    lines.append(f"edges {len(edges)}")
    lines += [f"{a} {b} {c:.1f}" for a, b, c in edges]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def read_graph(path):
    """(world name, nodes, edges) from a file written by write_graph."""
    with open(path) as handle:
        lines = iter(handle.read().splitlines())
    if next(lines) != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} file")
    world = next(lines).split(" ", 1)[1]
    next(lines)
    nodes = [tuple(map(float, next(lines).split())) for _ in range(int(next(lines).split()[1]))]
    edges = []
    for _ in range(int(next(lines).split()[1])):
        a, b, c = next(lines).split()
        edges.append((int(a), int(b), float(c)))
    return world, nodes, edges


# ---- Routes ----

def route(nodes, edges, start, goal, attach=6, reach=6000.0, detour=1.3):
    """A* over the gate graph from `start` to `goal`, both joined to their `attach` nearest gates
    within `reach` by straight lines costed `detour` times their length. The plugin does the same,
    except that it joins the start through the loaded navmeshes. Returns (points, cost) or None."""
    adjacency = [[] for _ in nodes]
    for a, b, c in edges:
        adjacency[a].append((b, c))
        adjacency[b].append((a, c))
    n = len(nodes)
    start_id, goal_id = n, n + 1
    points = list(nodes) + [tuple(start), tuple(goal)]
    adjacency += [[], []]

    def nearest(p):
        ranked = sorted(range(n), key=lambda i: _dist(nodes[i], p))
        return [i for i in ranked[:attach] if math.hypot(nodes[i][0] - p[0], nodes[i][1] - p[1]) <= reach]

    for i in nearest(start):
        adjacency[start_id].append((i, _dist(nodes[i], start) * detour))
    for i in nearest(goal):
        adjacency[i].append((goal_id, _dist(nodes[i], goal) * detour))
    if _dist(start, goal) <= reach:
        adjacency[start_id].append((goal_id, _dist(start, goal) * detour))
    best = {start_id: 0.0}
    parent = {}
    heap = [(_dist(start, goal) * PREFERRED_SCALE, start_id)]
    closed = set()
    while heap:
        _, u = heapq.heappop(heap)
        if u in closed:
            continue
        if u == goal_id:
            path = [u]
            while path[-1] in parent:
                path.append(parent[path[-1]])
            return [points[i] for i in reversed(path)], best[u]
        closed.add(u)
        for v, c in adjacency[u]:
            nxt = best[u] + c
            if nxt < best.get(v, math.inf):
                best[v] = nxt
                parent[v] = u
                heapq.heappush(heap, (nxt + _dist(points[v], goal) * PREFERRED_SCALE, v))
    return None


# ---- Command line ----

def _point(text):
    values = [float(v) for v in text.split(",")]
    if len(values) not in (2, 3):
        raise argparse.ArgumentTypeError("a point is X,Y or X,Y,Z")
    return tuple(values + [0.0] * (3 - len(values)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="write the gate graph of one worldspace")
    build.add_argument("--data", required=True, help="the game's Data folder")
    build.add_argument("--world", default="Tamriel", help="worldspace editor id (default Tamriel)")
    build.add_argument("--plugins", help="comma-separated load order (default: base masters and Creation Club files)")
    build.add_argument("-o", "--output", required=True)
    probe = commands.add_parser("probe", help="count navmeshes per worldspace")
    probe.add_argument("--data", required=True)
    probe.add_argument("--plugins")
    find = commands.add_parser("route", help="plan a route over a graph file")
    find.add_argument("graph")
    find.add_argument("--from", dest="start", type=_point, required=True)
    find.add_argument("--to", dest="goal", type=_point, required=True)
    options = parser.parse_args(argv)
    log = lambda text: print(text, file=sys.stderr, flush=True)

    if options.command == "route":
        _, nodes, edges = read_graph(options.graph)
        found = route(nodes, edges, options.start, options.goal)
        if not found:
            print("no route")
            return 1
        points, cost = found
        length = sum(_dist(a, b) for a, b in zip(points, points[1:]))
        print(f"{len(points)} points, length {length:.0f}, cost {cost:.0f}")
        for p in points:
            print(f"{p[0]:.0f},{p[1]:.0f},{p[2]:.0f}")
        return 0

    paths = load_order(options.data, options.plugins.split(",") if options.plugins else None)
    meshes, worlds = read_world_meshes(paths, log=log)
    if options.command == "probe":
        names = {v: k for k, v in worlds.items()}
        counts = {}
        for mesh in meshes.values():
            counts[mesh.world] = counts.get(mesh.world, 0) + 1
        for world, count in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
            print(f"{names.get(world, world)}: {count} navmeshes")
        return 0
    if options.world not in worlds:
        parser.error(f"no worldspace {options.world}")
    nodes, edges = build_graph(meshes, worlds[options.world], log=log)
    write_graph(options.output, options.world, nodes, edges, [p.name for p in paths])
    log(f"wrote {options.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
