"""Rooms of a cell: clusters of connected navmesh triangles split at chokepoints (SPDX-License-Identifier: GPL-3.0-or-later).

The plugin sends the raw triangles of the player's cell (Cell.navmesh), what lies in it (Cell.contents) and where the
player has stood (Cell.visited). Everything here is pure Python so it can be tested on synthetic meshes:

* `Mesh.from_reply` reads the triangle soup and its neighbour links;
* `segment` measures how far every triangle is from a wall and grows regions from the open spots downwards, keeping
  two regions apart where the way between them is much narrower than both (a chokepoint);
* `build_rooms` turns regions into rooms, folds narrow connecting bands into links (passages) and measures them;
* `build_model` places actors, containers, doors, activators and items in rooms, marks visited rooms and answers
  questions like "which room is this point in";
* `describe` / `plot_svg` render the model as compact text for an LLM and as a top-down picture.
"""

import heapq
import math
import re
from collections import Counter, defaultdict

# clearance (distance to the nearest wall) a region needs somewhere inside to count as a room rather than a tunnel
ROOM_PEAK = 150.0
# two regions stay apart when the saddle between them is under this share of the higher peak
SADDLE_RATIO = 0.2
# a dip between two peaks shallower than this (absolute, or share of the lower peak) is noise in the distance
# estimate, not a chokepoint
NOISE_ABS = 60.0
NOISE_REL = 0.25
# regions smaller than this (square units) are folded into their longest-bordering neighbour
MIN_REGION_AREA = 200000.0
# a region longer than this (walking distance end to end) is cut into bands: a snaking tunnel is not one place
MAX_SPAN = 3600.0
# a visited point reveals triangles this close to it
REVEAL_RADIUS = 380.0
REVEAL_DZ = 220.0
# a room counts as visited when this share of its area was revealed
VISITED_SHARE = 0.3
DOOR_LINK_RADIUS = 220.0
BUCKET = 256.0

LEVER_WORDS = re.compile(r"lever|chain|pull|switch|button|handle|wheel|crank|dial|keyhole|puzzle", re.I)
BOSS_WORDS = re.compile(r"overlord|warlord|chieftain|chief|leader|boss|lord|king|queen|necromancer|dragon priest|ancient|"
                        r"captain|commander|master", re.I)
LOCK_RANK = {"veryEasy": 1, "easy": 2, "average": 3, "hard": 4, "veryHard": 5, "requiresKey": 6}


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def seg_dist2(p, a, b):
    abx, aby = b[0] - a[0], b[1] - a[1]
    len2 = abx * abx + aby * aby
    t = 0.0 if len2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / len2))
    return math.hypot(p[0] - (a[0] + abx * t), p[1] - (a[1] + aby * t))


class Mesh:
    """Triangles with neighbour links. Triangle t has vertices tris[t] and, across edge i (vertex i to i+1), nbr[t][i]."""

    def __init__(self, verts, tris, nbr):
        self.verts = verts
        self.tris = tris
        self.nbr = nbr
        self.n = len(tris)
        self.center = []
        self.area = []
        for a, b, c in tris:
            pa, pb, pc = verts[a], verts[b], verts[c]
            self.center.append(((pa[0] + pb[0] + pc[0]) / 3, (pa[1] + pb[1] + pc[1]) / 3, (pa[2] + pb[2] + pc[2]) / 3))
            self.area.append(abs((pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0])) / 2)
        self._grid = None

    @classmethod
    def from_reply(cls, reply):
        v, t, n = reply["v"], reply["t"], reply["n"]
        verts = [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v), 3)]
        tris = [(t[i], t[i + 1], t[i + 2]) for i in range(0, len(t), 3)]
        nbr = [(n[i], n[i + 1], n[i + 2]) for i in range(0, len(n), 3)]
        return cls(verts, tris, nbr)

    def edge(self, t, i):
        a = self.verts[self.tris[t][i]]
        b = self.verts[self.tris[t][(i + 1) % 3]]
        return a, b

    def _build_grid(self):
        grid = defaultdict(list)
        for t in range(self.n):
            xs = [self.verts[i][0] for i in self.tris[t]]
            ys = [self.verts[i][1] for i in self.tris[t]]
            for gx in range(int(min(xs) // BUCKET), int(max(xs) // BUCKET) + 1):
                for gy in range(int(min(ys) // BUCKET), int(max(ys) // BUCKET) + 1):
                    grid[(gx, gy)].append(t)
        self._grid = grid

    def locate(self, p, max_dz=200.0, max_dist=350.0):
        """The triangle under point p (plan view, nearest in height), else the nearest centre within max_dist; None."""
        if self._grid is None:
            self._build_grid()
        gx, gy = int(p[0] // BUCKET), int(p[1] // BUCKET)
        best, best_dz = None, max_dz
        near, near_d = None, max_dist
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for t in self._grid.get((gx + dx, gy + dy), ()):
                    if t in seen:
                        continue
                    seen.add(t)
                    c = self.center[t]
                    d = dist3(c, p)
                    if d < near_d:
                        near, near_d = t, d
                    if dx == 0 and dy == 0 and _inside(self, t, p):
                        dz = abs(c[2] - p[2])
                        if dz < best_dz:
                            best, best_dz = t, dz
        return best if best is not None else near


def _inside(mesh, t, p):
    a, b, c = (mesh.verts[i] for i in mesh.tris[t])
    d0 = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    d1 = (c[0] - b[0]) * (p[1] - b[1]) - (c[1] - b[1]) * (p[0] - b[0])
    d2 = (a[0] - c[0]) * (p[1] - c[1]) - (a[1] - c[1]) * (p[0] - c[0])
    return not ((d0 < 0 or d1 < 0 or d2 < 0) and (d0 > 0 or d1 > 0 or d2 > 0))


def clearance(mesh, cap=1500.0):
    """Approximate distance from each triangle's centre to the nearest wall (an edge with no neighbour)."""
    clr = [cap] * mesh.n
    for t in range(mesh.n):
        for i in range(3):
            if mesh.nbr[t][i] < 0:
                a, b = mesh.edge(t, i)
                clr[t] = min(clr[t], seg_dist2(mesh.center[t], a, b))
    heap = [(c, t) for t, c in enumerate(clr)]
    heapq.heapify(heap)
    while heap:
        c, t = heapq.heappop(heap)
        if c > clr[t]:
            continue
        for u in mesh.nbr[t]:
            if u >= 0:
                step = c + dist2(mesh.center[t], mesh.center[u])
                if step < clr[u]:
                    clr[u] = step
                    heapq.heappush(heap, (step, u))
    return clr


def segment(mesh, clr, ratio=SADDLE_RATIO, h_abs=NOISE_ABS, h_rel=NOISE_REL):
    """Region label per triangle: grow from the most open triangles downwards; a triangle that joins two regions
    merges them only when it is at least `ratio` of the higher peak, otherwise it is a chokepoint between them."""
    parent = list(range(mesh.n))
    peak = list(clr)
    done = [False] * mesh.n

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for t in sorted(range(mesh.n), key=lambda i: -clr[i]):
        done[t] = True
        roots = sorted({find(u) for u in mesh.nbr[t] if u >= 0 and done[u]}, key=lambda r: -peak[r])
        if not roots:
            continue
        top = roots[0]
        parent[t] = top
        for other in roots[1:]:
            lo, hi = sorted((peak[top], peak[other]))
            if clr[t] >= ratio * hi or lo - clr[t] < max(h_abs, h_rel * lo):
                parent[other] = top
                peak[top] = max(peak[top], peak[other])
    return [find(t) for t in range(mesh.n)], peak


def _geodesic(mesh, members, source):
    dist = {source: 0.0}
    heap = [(0.0, source)]
    while heap:
        d, t = heapq.heappop(heap)
        if d > dist[t]:
            continue
        for u in mesh.nbr[t]:
            if u in members:
                step = d + dist2(mesh.center[t], mesh.center[u])
                if step < dist.get(u, math.inf):
                    dist[u] = step
                    heapq.heappush(heap, (step, u))
    return dist


def split_long(mesh, labels, max_span=MAX_SPAN):
    """Cut regions whose far ends are more than max_span apart into bands of equal walking distance from one end;
    a band that a loop makes disconnected becomes one region per piece."""
    labels = list(labels)
    next_label = max(labels) + 1 if labels else 0
    for region, tris in list(_regions(labels).items()):
        members = set(tris)
        first = _geodesic(mesh, members, tris[0])
        end = max(first, key=first.get)
        dist = _geodesic(mesh, members, end)
        span = max(dist.values())
        if span <= max_span:
            continue
        bands = math.ceil(span / max_span)
        band_of = {t: min(bands - 1, int(dist.get(t, 0.0) / (span / bands))) for t in tris}
        seen = set()
        for start in tris:
            if start in seen:
                continue
            piece, stack = [], [start]
            seen.add(start)
            while stack:
                t = stack.pop()
                piece.append(t)
                for u in mesh.nbr[t]:
                    if u in members and u not in seen and band_of[u] == band_of[start]:
                        seen.add(u)
                        stack.append(u)
            for t in piece:
                labels[t] = next_label
            next_label += 1
    return labels


class Room:
    def __init__(self, rid, tris, mesh):
        self.id = rid
        self.tris = tris
        area = sum(mesh.area[t] for t in tris) or 1.0
        self.area = area
        self.x = sum(mesh.center[t][0] * mesh.area[t] for t in tris) / area
        self.y = sum(mesh.center[t][1] * mesh.area[t] for t in tris) / area
        self.z = sum(mesh.center[t][2] * mesh.area[t] for t in tris) / area
        xs = [mesh.verts[i][0] for t in tris for i in mesh.tris[t]]
        ys = [mesh.verts[i][1] for t in tris for i in mesh.tris[t]]
        zs = [mesh.center[t][2] for t in tris]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self.zmin, self.zmax = min(zs), max(zs)
        # a walkable point to head for: the triangle centre nearest the centroid, since the centroid of a curved
        # cave can lie in rock
        best = min(tris, key=lambda t: dist2(mesh.center[t], (self.x, self.y)) + abs(mesh.center[t][2] - self.z))
        self.anchor = tuple(round(c) for c in mesh.center[best])
        self.level = 0
        self.links = {}  # other room id -> {"via": "open"|"passage"|"door", "width", "length", "door"}


def _regions(labels):
    out = defaultdict(list)
    for t, r in enumerate(labels):
        out[r].append(t)
    return out


def build_rooms(mesh, room_peak=ROOM_PEAK, ratio=SADDLE_RATIO, min_area=MIN_REGION_AREA, h_abs=NOISE_ABS, h_rel=NOISE_REL,
                max_span=MAX_SPAN):
    """Rooms and the links between them. Returns (rooms: dict id -> Room, owner: triangle -> room id or None for
    triangles in a passage between rooms, passages: list of {rooms, tris})."""
    if mesh.n == 0:
        return {}, [], []
    clr = clearance(mesh)
    labels, peak = segment(mesh, clr, ratio, h_abs, h_rel)
    labels = split_long(mesh, labels, max_span)
    regions = _regions(labels)

    def border(labels):
        """Triangle-adjacency length between regions: {(a, b): total shared edge length}."""
        out = Counter()
        for t in range(mesh.n):
            for i in range(3):
                u = mesh.nbr[t][i]
                if u > t and labels[u] != labels[t]:
                    a, b = mesh.edge(t, i)
                    out[tuple(sorted((labels[t], labels[u])))] += dist2(a, b)
        return out

    # fold regions too small to be anything into the neighbour they share the most border with
    while True:
        areas = {r: sum(mesh.area[t] for t in ts) for r, ts in regions.items()}
        small = [r for r in regions if areas[r] < min_area]
        if not small:
            break
        joined = False
        shared = border(labels)
        for r in sorted(small, key=lambda x: areas[x]):
            options = [(length, a if b == r else b) for (a, b), length in shared.items() if r in (a, b)]
            if options:
                target = max(options)[1]
                for t in regions[r]:
                    labels[t] = target
                regions[target].extend(regions.pop(r))
                joined = True
                break
        if not joined:
            break

    region_peak = {r: max(clr[t] for t in ts) for r, ts in regions.items()}
    is_room = {r: region_peak[r] >= room_peak for r in regions}
    shared = border(labels)
    neighbours = defaultdict(set)
    for (a, b) in shared:
        neighbours[a].add(b)
        neighbours[b].add(a)

    # connected bands of narrow regions
    band_of, bands = {}, []
    for r in regions:
        if is_room[r] or r in band_of:
            continue
        stack, members = [r], set()
        while stack:
            x = stack.pop()
            if x in members:
                continue
            members.add(x)
            stack.extend(y for y in neighbours[x] if not is_room[y])
        for x in members:
            band_of[x] = len(bands)
        bands.append(members)

    owner_region = {}  # region -> room region it belongs to (None: a passage)
    for r in regions:
        owner_region[r] = r if is_room[r] else None
    passages = []
    for members in bands:
        touching = {y for x in members for y in neighbours[x] if is_room[y]}
        if len(touching) == 0:
            # no big chamber near: the widest part of this tunnel network stands in as a room
            top = max(members, key=lambda x: region_peak[x])
            is_room[top] = True
            owner_region[top] = top
            for x in members - {top}:
                owner_region[x] = top
        elif len(touching) == 1:
            for x in members:
                owner_region[x] = next(iter(touching))
        else:
            passages.append({"regions": members, "touching": touching,
                             "tris": [t for x in members for t in regions[x]]})

    # triangles of the rooms (a dead-end tunnel belongs to the room it hangs off)
    room_tris = defaultdict(list)
    owner = [None] * mesh.n
    for r, ts in regions.items():
        o = owner_region[r]
        if o is not None:
            room_tris[o].extend(ts)
            for t in ts:
                owner[t] = o
    ordered = sorted(room_tris.values(), key=lambda ts: (-sum(mesh.area[t] for t in ts), min(ts)))
    rooms = {}
    region_to_id = {}
    for index, ts in enumerate(ordered, 1):
        rid = f"R{index}"
        rooms[rid] = Room(rid, ts, mesh)
        for t in ts:
            region_to_id[labels[t]] = rid
    owner = [region_to_id.get(labels[t]) if owner[t] is not None else None for t in range(mesh.n)]

    room_labels = [owner[t] for t in range(mesh.n)]
    # direct borders between rooms
    direct = Counter()
    for t in range(mesh.n):
        for i in range(3):
            u = mesh.nbr[t][i]
            if u > t and room_labels[t] and room_labels[u] and room_labels[t] != room_labels[u]:
                a, b = mesh.edge(t, i)
                direct[tuple(sorted((room_labels[t], room_labels[u])))] += dist2(a, b)
    for (a, b), width in direct.items():
        link = {"via": "open", "width": round(width), "length": 0, "tris": []}
        rooms[a].links[b] = dict(link)
        rooms[b].links[a] = dict(link)

    out_passages = []
    for band in passages:
        ts = band["tris"]
        ids = sorted({region_to_id[y] for y in band["touching"]})
        member = set(ts)
        costs = {}
        for rid in ids:
            costs[rid] = _band_costs(mesh, member, room_labels, rid)
        width = round(2 * max(clr[t] for t in ts))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                reach = [costs[a][t] + costs[b][t] for t in member if t in costs[a] and t in costs[b]]
                length = round(min(reach)) if reach else 0
                link = {"via": "passage", "width": width, "length": length, "tris": ts}
                if b not in rooms[a].links or rooms[a].links[b]["via"] == "open":
                    rooms[a].links[b] = dict(link)
                    rooms[b].links[a] = dict(link)
        out_passages.append({"rooms": ids, "tris": ts, "width": width})
    _assign_levels(rooms)
    return rooms, owner, out_passages


def _band_costs(mesh, member, room_labels, rid):
    """Walking cost inside a passage band from the triangles that touch room rid."""
    heap, cost = [], {}
    for t in member:
        if any(mesh.nbr[t][i] >= 0 and room_labels[mesh.nbr[t][i]] == rid for i in range(3)):
            cost[t] = 0.0
            heap.append((0.0, t))
    heapq.heapify(heap)
    while heap:
        c, t = heapq.heappop(heap)
        if c > cost.get(t, math.inf):
            continue
        for u in mesh.nbr[t]:
            if u in member:
                step = c + dist3(mesh.center[t], mesh.center[u])
                if step < cost.get(u, math.inf):
                    cost[u] = step
                    heapq.heappush(heap, (step, u))
    return cost


def _assign_levels(rooms, gap=220.0):
    zs = sorted({round(r.z) for r in rooms.values()})
    levels, current = {}, 0
    for i, z in enumerate(zs):
        if i and z - zs[i - 1] > gap:
            current += 1
        levels[z] = current
    for room in rooms.values():
        room.level = levels[round(room.z)]


def lock_text(entry):
    if entry.get("locked"):
        return "locked(" + str(entry.get("lockLevel", "?")) + ")"
    return ""


class CellModel:
    """Rooms plus what is in them and which have been seen."""

    def __init__(self, mesh, rooms, owner, passages, cell, interior):
        self.mesh = mesh
        self.rooms = rooms
        self.owner = owner
        self.passages = passages
        self.cell = cell
        self.interior = interior
        self.player = None
        self.player_room = None
        self.exit = None
        self.boss = None
        self.visited = {}      # room id -> share of area revealed
        self.reached = set()   # rooms the explorer arrived in
        self.contents = {}     # room id -> dict of lists
        self.all_actors = []
        self._passage_rooms = {}
        for band in passages:
            for t in band["tris"]:
                self._passage_rooms[t] = band["rooms"]

    # ---- placing points -------------------------------------------------------------------------------------
    def room_at(self, p):
        """Room id for a point; a point in a passage belongs to the nearer end. None when off the navmesh."""
        t = self.mesh.locate(p)
        if t is None:
            return None
        if self.owner[t] is not None:
            return self.owner[t]
        ends = self._passage_rooms.get(t)
        if ends:
            return min(ends, key=lambda rid: dist2((self.rooms[rid].x, self.rooms[rid].y), p))
        return None

    def nearest_room(self, p):
        found = self.room_at(p)
        if found or not self.rooms:
            return found
        return min(self.rooms, key=lambda rid: dist3((self.rooms[rid].x, self.rooms[rid].y, self.rooms[rid].z), p))

    def room_path(self, source, targets=None):
        """Walking distance between room anchors over the room graph: {room id: distance} from `source`."""
        dist = {source: 0.0}
        heap = [(0.0, source)]
        while heap:
            d, rid = heapq.heappop(heap)
            if d > dist.get(rid, math.inf):
                continue
            for other, link in self.rooms[rid].links.items():
                step = d + dist3(self.rooms[rid].anchor, self.rooms[other].anchor)
                if step < dist.get(other, math.inf):
                    dist[other] = step
                    heapq.heappush(heap, (step, other))
        return dist

    # ---- filling ------------------------------------------------------------------------------------------
    def mark_visits(self, points):
        """Reveal triangles near visited points and compute each room's revealed share."""
        mesh = self.mesh
        by_cell = defaultdict(list)
        for t in range(mesh.n):
            c = mesh.center[t]
            by_cell[(int(c[0] // BUCKET), int(c[1] // BUCKET))].append(t)
        revealed = set()
        span = int(REVEAL_RADIUS // BUCKET) + 1
        for p in points:
            gx, gy = int(p[0] // BUCKET), int(p[1] // BUCKET)
            for dx in range(-span, span + 1):
                for dy in range(-span, span + 1):
                    for t in by_cell.get((gx + dx, gy + dy), ()):
                        c = mesh.center[t]
                        if t not in revealed and abs(c[2] - p[2]) <= REVEAL_DZ and dist2(c, p) <= REVEAL_RADIUS:
                            revealed.add(t)
        for rid, room in self.rooms.items():
            total = sum(mesh.area[t] for t in room.tris) or 1.0
            self.visited[rid] = sum(mesh.area[t] for t in room.tris if t in revealed) / total

    def is_visited(self, rid):
        return rid in self.reached or self.visited.get(rid, 0.0) >= VISITED_SHARE or rid == self.player_room

    def add_contents(self, contents, player):
        player = player or contents.get("player")
        self.player = tuple(player) if player else None
        self.god_mode = bool(contents.get("godMode"))
        self.player_room = self.nearest_room(self.player) if self.player else None
        empty = lambda: {"hostiles": [], "neutral": [], "containers": [], "doors": [], "activators": [], "items": []}  # noqa: E731
        self.contents = {rid: empty() for rid in self.rooms}
        self.off_mesh = empty()
        actors = contents.get("actors", [])
        self.all_actors = actors
        levels = sorted(a.get("level", 1) for a in actors if a.get("hostile"))
        median = levels[len(levels) // 2] if levels else 0
        top = levels[-1] if levels else 0
        for actor in actors:
            if actor.get("hostile"):
                is_boss = bool(actor.get("unique") or BOSS_WORDS.search(actor.get("name") or "")) or \
                    (len(levels) >= 3 and actor.get("level", 1) >= top and top >= median + 6)
                actor["boss"] = is_boss
        bosses = [a for a in actors if a.get("boss")]
        if bosses:
            self.boss = max(bosses, key=lambda a: a.get("level", 1))
        for key, category in (("actors", None), ("containers", "containers"), ("doors", "doors"),
                              ("activators", "activators"), ("items", "items")):
            for entry in contents.get(key, []):
                pos = entry.get("pos")
                rid = self.nearest_room(pos) if pos else None
                slot = self.contents[rid] if rid in self.contents else self.off_mesh
                entry["room"] = rid
                if key == "actors":
                    slot["hostiles" if entry.get("hostile") else "neutral"].append(entry)
                else:
                    slot[category].append(entry)
        for rid, slot in self.contents.items():
            for door in slot["doors"]:
                if door.get("teleports"):
                    dest = door.get("destination") or {}
                    door["exit"] = dest.get("interior") is False
        self.exit = self._find_exit()
        self._flag_chests()
        self._door_links()

    def _find_exit(self):
        load = [d for slot in self.contents.values() for d in slot["doors"] if d.get("teleports")]
        exits = [d for d in load if d.get("exit")]
        if exits:
            return exits[0]
        unresolved = [d for d in load if not (d.get("destination") or {}).get("resolved")]
        if self.interior and len(unresolved) == 1 and len(load) == 1:
            unresolved[0]["exit"] = True
            unresolved[0]["exitGuess"] = True
            return unresolved[0]
        return None

    def _flag_chests(self):
        boss_room = self.boss.get("room") if self.boss else None
        for rid, slot in self.contents.items():
            near_boss = False
            if self.boss and self.boss.get("pos"):
                for chest in slot["containers"]:
                    if chest.get("pos") and dist3(chest["pos"], self.boss["pos"]) < 1100:
                        near_boss = True
            for chest in slot["containers"]:
                name = (chest.get("name") or "")
                if "chest" not in name.lower() and not chest.get("locked"):
                    continue
                strong = chest.get("locked") or chest.get("size", 0) >= 90 or chest.get("entries", 0) >= 6
                if re.search(r"boss", name, re.I) or (strong and (rid == boss_room or near_boss)):
                    chest["bossChest"] = True

    def _door_links(self):
        plain = [d for slot in self.contents.values() for d in slot["doors"] if not d.get("teleports") and d.get("pos")]
        if not plain:
            return
        mesh = self.mesh
        for room in self.rooms.values():
            for other, link in room.links.items():
                tris = link.get("tris") or []
                if not tris and link["via"] == "open":
                    tris = [t for t in room.tris if any(
                        mesh.nbr[t][i] >= 0 and self.owner[mesh.nbr[t][i]] == other for i in range(3))]
                for door in plain:
                    if any(dist3(mesh.center[t], door["pos"]) < DOOR_LINK_RADIUS for t in tris[:4000]):
                        link["door"] = door["refID"]
                        link["via"] = "door"
                        if door.get("locked"):
                            link["locked"] = door.get("lockLevel", True)
                        break

    # ---- queries ------------------------------------------------------------------------------------------
    def hostiles(self):
        return [a for slot in self.contents.values() for a in slot["hostiles"]]

    def unvisited(self):
        return [rid for rid in self.rooms if not self.is_visited(rid)]

    def find(self, text):
        """References whose name contains text (case-insensitive): [(kind, entry)]."""
        needle = text.lower()
        found = []
        for slot in list(self.contents.values()) + [self.off_mesh]:
            for key, kind in (("hostiles", "actor"), ("neutral", "actor"), ("containers", "container"), ("doors", "door"),
                              ("activators", "activator"), ("items", "item")):
                found.extend((kind, e) for e in slot[key] if needle in (e.get("name") or "").lower())
        return found


def build_model(navmesh, contents, visited=None, player=None, reached=(), **tuning):
    mesh = Mesh.from_reply(navmesh)
    rooms, owner, passages = build_rooms(mesh, **tuning)
    model = CellModel(mesh, rooms, owner, passages, navmesh.get("cell"), navmesh.get("interior", True))
    model.reached = set(reached)
    if visited:
        model.mark_visits(visited.get("points", []))
    model.add_contents(contents, player or navmesh.get("origin"))
    return model


# ---- text ----------------------------------------------------------------------------------------------------

def _names(entries, limit=4):
    counts = Counter(e.get("name") or "?" for e in entries)
    parts = []
    for name, count in counts.most_common(limit):
        levels = [e.get("level") for e in entries if (e.get("name") or "?") == name and e.get("level")]
        label = f"{name}{' x' + str(count) if count > 1 else ''}"
        if levels:
            label += f" L{min(levels)}" + (f"-{max(levels)}" if max(levels) != min(levels) else "")
        parts.append(label)
    if len(counts) > limit:
        parts.append(f"+{len(counts) - limit} more")
    return ", ".join(parts)


def room_summary(model, rid):
    """One room as a compact dict (for JSON output and the text renderer)."""
    room = model.rooms[rid]
    slot = model.contents.get(rid) or {}
    out = {
        "id": rid, "centre": [round(room.x), round(room.y), round(room.z)], "anchor": list(room.anchor),
        "size": [round(room.bbox[2] - room.bbox[0]), round(room.bbox[3] - room.bbox[1])], "area": round(room.area),
        "level": room.level, "z": [round(room.zmin), round(room.zmax)],
        "visited": model.is_visited(rid), "here": rid == model.player_room,
        "links": [{"room": other, **{k: v for k, v in link.items() if k not in ("tris",) and v not in (None, 0)}}
                  for other, link in sorted(room.links.items())],
    }
    hostiles = slot.get("hostiles", [])
    if hostiles:
        out["hostiles"] = {"count": len(hostiles), "who": _names(hostiles),
                           "maxLevel": max(h.get("level", 1) for h in hostiles)}
        boss = [h for h in hostiles if h.get("boss")]
        if boss:
            out["hostiles"]["boss"] = {"name": boss[0].get("name"), "refID": boss[0]["refID"], "level": boss[0].get("level")}
    if slot.get("neutral"):
        out["neutral"] = _names(slot["neutral"])
    if slot.get("containers"):
        out["containers"] = [{k: c[k] for k in ("refID", "name", "locked", "lockLevel", "bossChest", "size") if k in c and c[k] not in (None, False)}
                             for c in slot["containers"][:8]]
    if slot.get("doors"):
        out["doors"] = [{k: d[k] for k in ("refID", "name", "teleports", "exit", "exitGuess", "locked", "lockLevel") if k in d and d[k] not in (None, False)}
                        | ({"to": (d.get("destination") or {}).get("cell") or (d.get("destination") or {}).get("worldspace")}
                           if d.get("teleports") else {}) for d in slot["doors"]]
    if slot.get("activators"):
        out["activators"] = [{"refID": a["refID"], "name": a.get("name"),
                              "kind": "lever" if LEVER_WORDS.search(a.get("name") or "") else "activator"}
                             for a in slot["activators"][:8]]
    if slot.get("items"):
        top = sorted(slot["items"], key=lambda i: -(i.get("value") or 0))[:5]
        out["items"] = [{"refID": i["refID"], "name": i.get("name"), "value": i.get("value")} for i in top]
    return out


def cell_summary(model):
    cell = model.cell or {}
    hostiles = model.hostiles()
    return {
        "cell": cell.get("name") or cell.get("editorID"), "interior": model.interior,
        "rooms": len(model.rooms), "levels": 1 + max((r.level for r in model.rooms.values()), default=0),
        "triangles": model.mesh.n, "unvisited": len(model.unvisited()), "hostilesAlive": len(hostiles),
        "hostileRooms": sorted({h["room"] for h in hostiles if h.get("room")}),
        "boss": ({"name": model.boss.get("name"), "level": model.boss.get("level"), "room": model.boss.get("room"),
                  "refID": model.boss["refID"]} if model.boss else None),
        "exit": ({"refID": model.exit["refID"], "room": model.exit.get("room"), "name": model.exit.get("name"),
                  "guess": bool(model.exit.get("exitGuess"))} if model.exit else None),
        "playerRoom": model.player_room,
    }


def describe(model):
    """The map as text: one header, one line per room."""
    summary = cell_summary(model)
    head = (f"{summary['cell']} ({'interior' if model.interior else 'exterior'}): {summary['rooms']} rooms, {summary['levels']} level(s), "
            f"{summary['unvisited']} unvisited, {summary['hostilesAlive']} hostile alive; you are in {summary['playerRoom']}")
    if summary["exit"]:
        head += f"; exit door {summary['exit']['refID']} in {summary['exit']['room']}" + (" (guess)" if summary["exit"]["guess"] else "")
    if summary["boss"]:
        head += f"; likely boss {summary['boss']['name']} L{summary['boss']['level']} in {summary['boss']['room']}"
    lines = [head]
    for rid in model.rooms:
        s = room_summary(model, rid)
        bits = [f"{rid} @({s['centre'][0]},{s['centre'][1]},{s['centre'][2]}) {s['size'][0]}x{s['size'][1]} L{s['level']}",
                "HERE" if s["here"] else ("visited" if s["visited"] else "unvisited")]
        links = ", ".join(f"{link['room']}:{link['via']}" + (f" {link['length']}u" if link.get("length") else "")
                          + (f" locked" if link.get("locked") else "") for link in s["links"])
        bits.append("to " + (links or "-"))
        if "hostiles" in s:
            h = s["hostiles"]
            bits.append(f"hostile {h['count']}: {h['who']}" + (f" [BOSS {h['boss']['name']}]" if "boss" in h else ""))
        if s.get("neutral"):
            bits.append(f"neutral: {s['neutral']}")
        for c in s.get("containers", []):
            bits.append(f"{'BOSS CHEST ' if c.get('bossChest') else ''}{c.get('name')} {c['refID']}" + (f" locked({c.get('lockLevel', '?')})" if c.get("locked") else ""))
        for d in s.get("doors", []):
            tag = "EXIT " if d.get("exit") else ("load door " if d.get("teleports") else "door ")
            bits.append(f"{tag}{d.get('name')} {d['refID']}" + (f" -> {d['to']}" if d.get("to") else "") + (f" locked({d.get('lockLevel', '?')})" if d.get("locked") else ""))
        for a in s.get("activators", []):
            bits.append(f"{a['kind']} {a['name']} {a['refID']}")
        if s.get("items"):
            bits.append("items: " + ", ".join(f"{i['name']}({i['value']})" for i in s["items"]))
        lines.append(" | ".join(bits))
    if model.off_mesh and any(model.off_mesh.values()):
        extra = {k: len(v) for k, v in model.off_mesh.items() if v}
        lines.append(f"off navmesh: {extra}")
    return "\n".join(lines)


# ---- picture -------------------------------------------------------------------------------------------------

PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#edc948", "#76b7b2", "#ff9da7", "#9c755f", "#86bcb6", "#d37295"]


def plot_svg(model, title="cell", visited_points=(), size=1100):
    mesh = model.mesh
    if not mesh.n:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    xs = [v[0] for v in mesh.verts]
    ys = [v[1] for v in mesh.verts]
    pad = 60
    scale = (size - 2 * pad) / max(max(xs) - min(xs), max(ys) - min(ys), 1)
    width = round((max(xs) - min(xs)) * scale + 2 * pad)
    height = round((max(ys) - min(ys)) * scale + 2 * pad)

    def px(p):
        return round((p[0] - min(xs)) * scale + pad, 1), round((max(ys) - p[1]) * scale + pad, 1)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="sans-serif">',
           f'<rect width="100%" height="100%" fill="#111"/><text x="10" y="20" fill="#ddd" font-size="14">{title}</text>']
    ids = list(model.rooms)
    color = {rid: PALETTE[i % len(PALETTE)] for i, rid in enumerate(ids)}
    for t in range(mesh.n):
        rid = model.owner[t]
        fill = color[rid] if rid else "#555"
        opacity = 0.75 if (rid is None or model.is_visited(rid)) else 0.38
        pts = " ".join("%s,%s" % px(mesh.verts[i]) for i in mesh.tris[t])
        out.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="{opacity}" stroke="{fill}" stroke-width="0.3"/>')
    for p in visited_points:
        x, y = px(p)
        out.append(f'<circle cx="{x}" cy="{y}" r="1.6" fill="#fff" fill-opacity="0.6"/>')
    for rid, room in model.rooms.items():
        for other in room.links:
            if rid < other:
                a, b = px(room.anchor), px(model.rooms[other].anchor)
                dash = "" if room.links[other]["via"] != "door" else ' stroke-dasharray="4 3"'
                out.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#fff" stroke-opacity="0.35"{dash}/>')
    for rid, room in model.rooms.items():
        x, y = px(room.anchor)
        out.append(f'<text x="{x}" y="{y}" fill="#fff" font-size="13" text-anchor="middle" stroke="#000" stroke-width="0.4">{rid}</text>')
    slots = list(model.contents.values()) + [model.off_mesh]
    for slot in slots:
        for key, fill, r in (("hostiles", "#ff3b30", 4), ("containers", "#ffd60a", 3.5), ("activators", "#bf5af2", 3)):
            for e in slot[key]:
                if e.get("pos"):
                    x, y = px(e["pos"])
                    out.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="#000" stroke-width="0.6"/>')
        for e in slot["doors"]:
            if e.get("pos"):
                x, y = px(e["pos"])
                fill = "#30d158" if e.get("exit") else ("#0a84ff" if e.get("teleports") else "#8e8e93")
                out.append(f'<rect x="{x - 4}" y="{y - 4}" width="8" height="8" fill="{fill}" stroke="#000" stroke-width="0.6"/>')
    if model.player:
        x, y = px(model.player)
        out.append(f'<circle cx="{x}" cy="{y}" r="6" fill="none" stroke="#fff" stroke-width="2"/>')
    out.append("</svg>")
    return "\n".join(out)
