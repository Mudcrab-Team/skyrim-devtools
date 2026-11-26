"""The navigation graph generator on hand-made toy plugins (no game data)."""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from skyrim_devtools import navgraph

WORLD = 0x3C


def sub(tag, data):
    return tag + struct.pack("<H", len(data)) + data


def record(tag, form_id, payload, flags=0):
    if flags & navgraph.FLAG_COMPRESSED:
        payload = struct.pack("<I", len(payload)) + zlib.compress(payload)
    return struct.pack("<4sIIIIHH", tag, len(payload), flags, form_id, 0, 44, 0) + payload


def group(label, kind, body):
    return struct.pack("<4sI4siHHI", b"GRUP", 24 + len(body), label, kind, 0, 0, 0) + body


def square(index, left, right, link_size):
    """A 1000x1000 mesh of two triangles at x = index*1000; `left`/`right` are (mesh id, triangle)
    of the neighbour across that side, or None."""
    x0 = index * 1000.0
    verts = [(x0, 0, 0), (x0 + 1000, 0, 0), (x0 + 1000, 1000, 0), (x0, 1000, 0)]
    links = []
    tri0 = [0, 1, 2, -1, -1, 1, 0]
    tri1 = [0, 2, 3, 0, -1, -1, 0]
    if right:
        tri0[4] = len(links)
        tri0[6] |= 0x2
        links.append(right)
    if left:
        tri1[5] = len(links)
        tri1[6] |= 0x4
        links.append(left)
    tri0[6] |= navgraph.TRI_PREFERRED
    blob = struct.pack("<III", 12, 0xA5E9A03C, WORLD) + struct.pack("<hh", 0, index)
    blob += struct.pack("<I", len(verts)) + b"".join(struct.pack("<3f", *v) for v in verts)
    blob += struct.pack("<I", 2) + b"".join(struct.pack("<HHHhhhHH", *t, 0) for t in (tri0, tri1))
    blob += struct.pack("<I", len(links))
    for mesh, tri in links:
        blob += struct.pack("<IIh", 0, mesh, tri) + (b"\xff" if link_size == 11 else b"")
    blob += struct.pack("<I", 0) + struct.pack("<I", 0)  # no door or cover triangles
    blob += struct.pack("<I", 1) + struct.pack("<2f", 1000, 1000) + struct.pack("<6f", x0, 0, 0, x0 + 1000, 1000, 0)
    blob += struct.pack("<I", 2) + struct.pack("<hh", 0, 1)  # one search-grid cell holding both triangles
    return sub(b"NVNM", blob)


def plugin(path, masters, body):
    header = b"".join(sub(b"MAST", m.encode() + b"\0") + sub(b"DATA", b"\0" * 8) for m in masters)
    Path(path).write_bytes(record(b"TES4", 0, sub(b"HEDR", struct.pack("<fII", 1.7, 0, 0)) + header) + body)


def world(meshes, base=0):
    edid = sub(b"EDID", b"Toy\0")
    return group(b"WRLD", 0, record(b"WRLD", base | WORLD, edid)
                 + group(struct.pack("<I", base | WORLD), 1, group(b"\0\0\0\0", 4, b"".join(meshes))))


def three(link_size):
    a, b, c = 0x100, 0x101, 0x102
    return [record(b"NAVM", a, square(0, None, (b, 1), link_size)),
            record(b"NAVM", b, square(1, (a, 0), (c, 1), link_size), flags=navgraph.FLAG_COMPRESSED),
            record(b"NAVM", c, square(2, (b, 0), None, link_size))]


class NavGraphTests(unittest.TestCase):
    def build(self, folder, link_size=11, override=None):
        data = Path(folder)
        plugin(data / "Base.esm", [], world(three(link_size)))
        names = ["Base.esm"]
        if override is not None:
            plugin(data / "Patch.esm", ["Base.esm"], world(override))
            names.append("Patch.esm")
        meshes, worlds = navgraph.read_world_meshes(navgraph.load_order(data, names), log=lambda _: None)
        return navgraph.build_graph(meshes, worlds["Toy"], log=lambda _: None)

    def test_gates_and_costs(self):
        for link_size in (10, 11):
            with tempfile.TemporaryDirectory() as folder:
                nodes, edges = self.build(folder, link_size)
            self.assertEqual(sorted(n[:2] for n in nodes), [(1000.0, 500.0), (2000.0, 500.0)], link_size)
            self.assertEqual(len(edges), 1)
            # the middle mesh joins its two gates: about 1000 units, partly over a preferred triangle
            self.assertTrue(700 < edges[0][2] < 1300, edges)

    def test_override_deletes_a_mesh(self):
        deleted = record(b"NAVM", 0x102, b"", flags=navgraph.FLAG_DELETED)
        with tempfile.TemporaryDirectory() as folder:
            nodes, edges = self.build(folder, override=[deleted])
        self.assertEqual([n[:2] for n in nodes], [(1000.0, 500.0)])
        self.assertEqual(edges, [])

    def test_write_read_route(self):
        with tempfile.TemporaryDirectory() as folder:
            nodes, edges = self.build(folder)
            path = Path(folder) / "Toy.navg"
            navgraph.write_graph(path, "Toy", nodes, edges, ["Base.esm"])
            name, nodes2, edges2 = navgraph.read_graph(path)
        self.assertEqual(name, "Toy")
        self.assertEqual(len(nodes2), 2)
        points, cost = navgraph.route(nodes2, edges2, (200, 500, 0), (2800, 500, 0), reach=900)
        self.assertEqual([p[:2] for p in points], [(200, 500), (1000, 500), (2000, 500), (2800, 500)])
        self.assertGreater(cost, 2000)
        self.assertIsNone(navgraph.route(nodes2, edges2, (200, 500, 0), (9000, 500, 0), reach=900))


if __name__ == "__main__":
    unittest.main()
