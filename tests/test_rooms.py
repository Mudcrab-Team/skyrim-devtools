import json
import unittest

from skyrim_devtools import rooms
from skyrim_devtools.use import explore
import test_use
from test_use import Harness, run

SIZE = 100


def grid_reply(cells, z=0, size=SIZE, base=None):
    """A navmesh reply (Cell.navmesh shape) for a set of (i, j) squares, each split into two triangles."""
    verts, index, tris = list(base["verts"]) if base else [], {}, list(base["tris"]) if base else []
    for k, v in enumerate(verts):
        index[v] = k

    def vid(x, y):
        key = (x * size, y * size, z)
        if key not in index:
            index[key] = len(verts)
            verts.append(key)
        return index[key]

    for i, j in sorted(cells):
        a, b, c, d = vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)
        tris.extend([(a, b, c), (a, c, d)])
    return {"verts": verts, "tris": tris}


def finish(parts):
    verts, tris = parts["verts"], parts["tris"]
    edges = {}
    for t, tri in enumerate(tris):
        for i in range(3):
            key = tuple(sorted((tri[i], tri[(i + 1) % 3])))
            edges.setdefault(key, []).append((t, i))
    nbr = [[-1, -1, -1] for _ in tris]
    for pairs in edges.values():
        if len(pairs) == 2:
            (t0, i0), (t1, i1) = pairs
            nbr[t0][i0], nbr[t1][i1] = t1, t0
    return {"cell": {"formId": "0001", "name": "Test Mine", "interior": True}, "interior": True, "origin": [0, 0, 0],
            "triangles": len(tris), "v": [c for v in verts for c in v], "t": [c for t in tris for c in t],
            "n": [c for n in nbr for c in n]}


def block(x0, y0, w, h):
    return {(i, j) for i in range(x0, x0 + w) for j in range(y0, y0 + h)}


def two_rooms():
    # 12x12 chamber, a 2-wide corridor 10 long, another 12x12 chamber
    cells = block(0, 0, 12, 12) | block(12, 5, 10, 2) | block(22, 0, 12, 12)
    return finish(grid_reply(cells))


def actor(ref, name, x, y, level=5, hostile=True, **extra):
    return {"refID": ref, "name": name, "pos": [x, y, 0], "level": level, "hostile": hostile, **extra}


def empty_contents(**extra):
    base = {"cell": {"formId": "0001", "name": "Test Mine"}, "interior": True, "player": [600, 600, 0], "actors": [],
            "containers": [], "doors": [], "activators": [], "items": []}
    base.update(extra)
    return base


class RoomTests(unittest.TestCase):
    def test_two_chambers_and_a_corridor_are_two_linked_rooms(self):
        model = rooms.build_model(two_rooms(), empty_contents())
        self.assertEqual(len(model.rooms), 2)
        a, b = model.rooms["R1"], model.rooms["R2"]
        self.assertIn(b.id, a.links)
        self.assertIn(a.id, b.links)
        self.assertLess(a.links["R2"]["width"], 300)
        xs = sorted(r.x for r in model.rooms.values())
        self.assertLess(xs[0], 800)
        self.assertGreater(xs[1], 2500)

    def test_one_open_hall_with_a_pillar_is_one_room(self):
        cells = block(0, 0, 14, 14) - block(6, 6, 2, 2)
        model = rooms.build_model(finish(grid_reply(cells)), empty_contents())
        self.assertEqual(len(model.rooms), 1)

    def test_dead_end_tunnel_belongs_to_its_room(self):
        cells = block(0, 0, 12, 12) | block(12, 5, 8, 2)
        model = rooms.build_model(finish(grid_reply(cells)), empty_contents())
        self.assertEqual(len(model.rooms), 1)
        self.assertGreater(model.rooms["R1"].bbox[2], 1800)

    def test_a_cave_that_is_all_tunnel_still_gets_a_room(self):
        cells = block(0, 0, 20, 2) | block(18, 2, 2, 10)
        model = rooms.build_model(finish(grid_reply(cells)), empty_contents())
        self.assertEqual(len(model.rooms), 1)

    def test_three_chambers_in_a_row_chain_their_links(self):
        cells = block(0, 0, 10, 10) | block(10, 4, 6, 2) | block(16, 0, 10, 10) | block(26, 4, 6, 2) | block(32, 0, 10, 10)
        model = rooms.build_model(finish(grid_reply(cells)), empty_contents())
        self.assertEqual(len(model.rooms), 3)
        middle = min(model.rooms.values(), key=lambda r: abs(r.x - 2100))
        self.assertEqual(len(middle.links), 2)

    def test_separate_floors_get_levels(self):
        low = grid_reply(block(0, 0, 10, 10), z=0)
        both = grid_reply(block(0, 0, 10, 10), z=500, base=low)
        model = rooms.build_model(finish(both), empty_contents(player=[500, 500, 0]))
        self.assertEqual(len(model.rooms), 2)
        self.assertEqual(sorted(r.level for r in model.rooms.values()), [0, 1])

    def test_locate_prefers_the_floor_the_point_stands_on(self):
        low = grid_reply(block(0, 0, 10, 10), z=0)
        both = grid_reply(block(0, 0, 10, 10), z=500, base=low)
        mesh = rooms.Mesh.from_reply(finish(both))
        t_low, t_high = mesh.locate((500, 500, 10)), mesh.locate((500, 500, 490))
        self.assertLess(mesh.center[t_low][2], 100)
        self.assertGreater(mesh.center[t_high][2], 400)
        self.assertIsNone(mesh.locate((9000, 9000, 0)))


class ModelTests(unittest.TestCase):
    def model(self, **contents):
        return rooms.build_model(two_rooms(), empty_contents(**contents), visited={"points": [[600, 600, 0]]},
                                 player=[600, 600, 0])

    def test_visited_marks_the_room_the_player_stood_in(self):
        model = self.model()
        left = model.room_at((600, 600, 0))
        right = model.room_at((2800, 600, 0))
        self.assertTrue(model.is_visited(left))
        self.assertFalse(model.is_visited(right))
        self.assertEqual(model.unvisited(), [right])
        self.assertEqual(model.player_room, left)

    def test_contents_land_in_their_rooms(self):
        model = self.model(actors=[actor("A1", "Bandit", 2800, 500), actor("A2", "Bandit", 2900, 700, level=7),
                                   actor("A3", "Prisoner", 300, 300, hostile=False)],
                           containers=[{"refID": "C1", "name": "Chest", "pos": [2800, 900, 0], "locked": True, "lockLevel": "hard", "size": 100}],
                           doors=[{"refID": "D1", "name": "Mine Exit", "pos": [100, 600, 0], "teleports": True,
                                   "destination": {"resolved": True, "interior": False, "worldspace": "Tamriel"}}],
                           activators=[{"refID": "L1", "name": "Pull Chain", "pos": [2000, 600, 0]}],
                           items=[{"refID": "I1", "name": "Ruby", "pos": [2700, 300, 0], "value": 300}])
        right, left = model.room_at((2800, 600, 0)), model.room_at((600, 600, 0))
        self.assertEqual(len(model.contents[right]["hostiles"]), 2)
        self.assertEqual(len(model.contents[left]["neutral"]), 1)
        self.assertEqual(model.exit["refID"], "D1")
        self.assertEqual(model.exit["room"], left)
        summary = rooms.room_summary(model, right)
        self.assertEqual(summary["hostiles"]["count"], 2)
        self.assertEqual(summary["hostiles"]["maxLevel"], 7)
        self.assertEqual(summary["items"][0]["name"], "Ruby")
        text = rooms.describe(model)
        self.assertIn("EXIT Mine Exit D1", text)
        self.assertIn("Bandit x2", text)
        self.assertIn(f"you are in {left}", text)

    def test_boss_and_boss_chest(self):
        boss = actor("B1", "Draugr Overlord", 2800, 600, level=20)
        grunts = [actor(f"G{i}", "Draugr", 2700 + i * 50, 400, level=6) for i in range(3)]
        model = self.model(actors=grunts + [boss],
                           containers=[{"refID": "C1", "name": "Dragonborn Chest", "pos": [3000, 900, 0], "locked": True, "size": 90},
                                       {"refID": "C2", "name": "Barrel", "pos": [200, 200, 0], "size": 40},
                                       {"refID": "C3", "name": "Chest", "pos": [200, 900, 0], "size": 60}])
        self.assertEqual(model.boss["refID"], "B1")
        flat = [c for slot in model.contents.values() for c in slot["containers"]]
        flags = {c["refID"]: c.get("bossChest") for c in flat}
        self.assertTrue(flags["C1"])
        self.assertFalse(flags["C2"])
        self.assertFalse(flags["C3"])

    def test_a_lone_load_door_is_guessed_as_the_exit(self):
        model = self.model(doors=[{"refID": "D1", "name": "Out", "pos": [100, 600, 0], "teleports": True,
                                   "destination": {"resolved": False}}])
        self.assertEqual(model.exit["refID"], "D1")
        self.assertTrue(model.exit["exitGuess"])

    def test_a_plain_door_marks_the_link_as_a_door(self):
        bare = self.model()
        border = next(t for t in bare.rooms["R1"].tris if any(
            u >= 0 and bare.owner[u] == "R2" for u in bare.mesh.nbr[t]))
        spot = list(bare.mesh.center[border])
        model = self.model(doors=[{"refID": "D2", "name": "Wooden Door", "pos": spot, "teleports": False,
                                   "locked": True, "lockLevel": "easy"}])
        link = model.rooms["R1"].links["R2"]
        self.assertEqual(link["via"], "door")
        self.assertEqual(link["door"], "D2")
        self.assertEqual(link["locked"], "easy")

    def test_svg_has_a_polygon_per_triangle_and_the_markers(self):
        model = self.model(actors=[actor("A1", "Bandit", 2800, 500)])
        svg = rooms.plot_svg(model, title="Test", visited_points=[[600, 600, 0]])
        self.assertEqual(svg.count("<polygon"), model.mesh.n)
        self.assertIn("#ff3b30", svg)
        self.assertTrue(svg.startswith("<svg"))


class PolicyTests(unittest.TestCase):
    def model(self, **contents):
        return rooms.build_model(two_rooms(), empty_contents(**contents), visited={"points": [[600, 600, 0]]}, player=[600, 600, 0])

    def test_unvisited_goes_to_the_other_room_then_is_done(self):
        model = self.model()
        memory = explore.Memory()
        pick = explore.choose_target(model, "unvisited", memory)
        self.assertEqual((pick["status"], pick["kind"], pick["room"]), ("go", "room", "R2" if model.player_room == "R1" else "R1"))
        memory.reached.add(pick["room"])
        model.reached = set(memory.reached)
        self.assertEqual(explore.choose_target(model, "unvisited", memory)["status"], "fully_explored")

    def test_clear_walks_to_the_nearest_hostile_and_reports_cleared(self):
        model = self.model(actors=[actor("A1", "Bandit", 2800, 500), actor("A2", "Bandit", 900, 900)])
        pick = explore.choose_target(model, "clear", explore.Memory())
        self.assertEqual((pick["kind"], pick["ref"]), ("ref", "A2"))
        cleared = explore.choose_target(self.model(), "clear", explore.Memory())
        self.assertEqual(cleared["status"], "cleared")
        self.assertEqual(cleared["unvisitedRooms"], 1)

    def test_clear_skips_blocked_hostiles(self):
        model = self.model(actors=[actor("A1", "Bandit", 2800, 500)])
        memory = explore.Memory()
        memory.blocked.add("ref:A1")
        self.assertEqual(explore.choose_target(model, "clear", memory)["status"], "cleared")

    def test_loot_visits_each_container_once(self):
        model = self.model(containers=[{"refID": "C1", "name": "Chest", "pos": [2800, 900, 0], "size": 70},
                                       {"refID": "C2", "name": "Sack", "pos": [900, 900, 0], "size": 30}])
        memory = explore.Memory()
        first = explore.choose_target(model, "loot", memory)
        self.assertEqual(first["ref"], "C2")
        memory.approached.add("C2")
        self.assertEqual(explore.choose_target(model, "loot", memory)["ref"], "C1")
        memory.approached.add("C1")
        self.assertEqual(explore.choose_target(model, "loot", memory)["status"], "looted")

    def test_exit_and_missing_exit(self):
        door = {"refID": "D1", "name": "Out", "pos": [2800, 600, 0], "teleports": True,
                "destination": {"resolved": True, "interior": False}}
        self.assertEqual(explore.choose_target(self.model(doors=[door]), "exit", explore.Memory())["ref"], "D1")
        near = dict(door, pos=[650, 600, 0])
        self.assertEqual(explore.choose_target(self.model(doors=[near]), "exit", explore.Memory())["status"], "at_exit")
        self.assertEqual(explore.choose_target(self.model(), "exit", explore.Memory())["status"], "no_exit")

    def test_find_by_name_then_search_then_not_found(self):
        model = self.model(items=[{"refID": "I1", "name": "Golden Claw", "pos": [2800, 300, 0], "value": 100}])
        pick = explore.choose_target(model, "find:claw", explore.Memory())
        self.assertEqual((pick["status"], pick["ref"]), ("go", "I1"))
        searching = explore.choose_target(model, "find:horn", explore.Memory())
        self.assertEqual(searching["kind"], "room")
        memory = explore.Memory()
        memory.reached.update(model.rooms)
        model.reached = set(memory.reached)
        self.assertEqual(explore.choose_target(model, "find:horn", memory)["status"], "not_found")

    def test_bad_goal_is_rejected(self):
        with self.assertRaises(ValueError):
            explore.choose_target(self.model(), "wander", explore.Memory())


class HintTests(unittest.TestCase):
    def test_no_target_suggests_explore(self):
        self.assertIn("explore", explore.quest_hint({"total": 0, "targets": []})["hint"])
        self.assertFalse(explore.quest_hint({"targets": []})["inThisCell"])

    def test_target_in_reach_gives_no_hint(self):
        reply = {"targets": [{"target": {"sameSpace": False}, "tracking": {"sameSpace": True}}]}
        self.assertEqual(explore.quest_hint(reply), {})

    def test_only_foreign_targets_suggest_exit(self):
        reply = {"targets": [{"target": {"sameSpace": False}, "tracking": None}]}
        self.assertIn("goal exit", explore.quest_hint(reply)["hint"])


TOOL_METHODS = ["Cell.navmesh", "Cell.contents", "Cell.visited", "Nav.goto", "Nav.status", "Nav.cancel", "Console.execute", "Quest.targets"]


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.saved = list(test_use.METHODS)
        test_use.METHODS.extend(TOOL_METHODS)

    def tearDown(self):
        test_use.METHODS[:] = self.saved

    async def scenario(self, contents, visited=None):
        h = Harness()
        await h.text("game_start")
        socket = h.sockets[0]
        socket.results["Cell.navmesh"] = two_rooms()
        socket.results["Cell.contents"] = contents
        socket.results["Cell.visited"] = visited or {"points": [[600, 600, 0]]}
        socket.results["Nav.status"] = {"active": False, "last": {"state": "arrived"}}
        return h, socket

    def test_cell_map_text_and_json(self):
        async def go():
            h, socket = await self.scenario(empty_contents(actors=[actor("A1", "Bandit", 2800, 500)]))
            text = await h.text("cell_map")
            self.assertIn("2 rooms", text)
            self.assertIn("hostile 1: Bandit", text)
            data = json.loads(await h.text("cell_map", format="json"))
            self.assertEqual(data["rooms"], 2)
            self.assertEqual(len(data["roomList"]), 2)
            self.assertEqual(data["hostilesAlive"], 1)
            await h.text("game_stop")
        run(go())

    def test_cell_map_writes_a_plot(self):
        async def go():
            import tempfile
            h, socket = await self.scenario(empty_contents())
            h.manager.config["cellMapsDir"] = tempfile.mkdtemp()
            text = await h.text("cell_map", plot=True)
            path = text.split("plot: ")[1].strip()
            self.assertTrue(open(path).read().startswith("<svg"))
            await h.text("game_stop")
        run(go())

    def test_explore_walks_to_the_unvisited_room_then_budget(self):
        async def go():
            h, socket = await self.scenario(empty_contents())
            reply = json.loads(await h.text("explore"))
            goto = [c for c in socket.calls if c[0] == "Nav.goto"]
            self.assertEqual(len(goto), 1)
            self.assertEqual(set(goto[0][1]["goal"]), {"x", "y", "z"})
            self.assertGreater(goto[0][1]["goal"]["x"], 2000)
            self.assertEqual(reply["legs"][0]["outcome"], "arrived")
            self.assertEqual(reply["status"], "fully_explored")
            again = json.loads(await h.text("explore", rooms=5))
            self.assertEqual(again["status"], "fully_explored")
            self.assertEqual(again["legs"], [])
            await h.text("game_stop")
        run(go())

    def test_explore_stops_at_the_leg_budget(self):
        async def go():
            h, socket = await self.scenario(empty_contents())
            cells = block(0, 0, 10, 10) | block(10, 4, 6, 2) | block(16, 0, 10, 10) | block(26, 4, 6, 2) | block(32, 0, 10, 10)
            socket.results["Cell.navmesh"] = finish(grid_reply(cells))
            reply = json.loads(await h.text("explore", rooms=1))
            self.assertEqual(reply["status"], "budget")
            self.assertEqual(len(reply["legs"]), 1)
            self.assertIn("next", reply)
            await h.text("game_stop")
        run(go())

    def test_explore_clear_reports_cleared_when_no_hostile(self):
        async def go():
            h, socket = await self.scenario(empty_contents())
            reply = json.loads(await h.text("explore", goal="clear", rooms=5))
            self.assertEqual(reply["status"], "cleared")
            self.assertEqual([c for c in socket.calls if c[0] == "Nav.goto"], [])
            await h.text("game_stop")
        run(go())

    def test_explore_gives_up_on_an_unreachable_hostile(self):
        async def go():
            h, socket = await self.scenario(empty_contents(actors=[actor("A1", "Bandit", 2800, 500)]))
            socket.results["Nav.status"] = {"active": False, "last": {"state": "failed", "reason": "no route"}}
            reply = json.loads(await h.text("explore", goal="clear", rooms=9))
            self.assertEqual(reply["status"], "cleared")
            self.assertTrue(reply["legs"][-1]["givenUp"])
            self.assertEqual(len(reply["legs"]), 2)
            await h.text("game_stop")
        run(go())

    def test_explore_fights_on_contact_then_resumes(self):
        async def go():
            h, socket = await self.scenario(empty_contents(actors=[actor("A1", "Bandit", 2800, 500)]))
            statuses = iter([{"active": False, "last": {"state": "failed", "reason": "Interrupted by combat"}},
                             {"active": False, "last": {"state": "arrived"}}])
            real_send = socket.send

            async def send(text):
                request = json.loads(text)
                if request["method"] == "Nav.goto" and request["params"].get("goal"):
                    socket.results["Nav.status"] = next(statuses, {"active": False, "last": {"state": "arrived"}})
                await real_send(text)
            socket.send = send
            socket.results["Combat.state"] = {"player": {"dead": False, "health": {"pct": 100}}, "hostiles": [], "hostileTotal": 0}
            fought = []

            async def fake_fight(**kwargs):
                fought.append(kwargs)
                return json.dumps({"outcome": "clear", "rounds": 3, "lowestHealthPct": 80, "hostilesLeft": 0})
            h.server._tool_manager._tools["combat_fight"].fn = fake_fight
            reply = json.loads(await h.text("explore", goal="clear", fight=True, invulnerable=True, rooms=3))
            self.assertEqual(len(fought), 1)
            self.assertEqual(reply["legs"][0]["fights"][0]["outcome"], "clear")
            goto = [c for c in socket.calls if c[0] == "Nav.goto"]
            self.assertEqual(goto[0][1]["onCombat"], "stop")
            await h.text("game_stop")
        run(go())

    def test_quest_targets_adds_the_explore_hint(self):
        async def go():
            h, socket = await self.scenario(empty_contents())
            socket.results["Quest.targets"] = {"total": 0, "targets": []}
            reply = json.loads(await h.text("quest_targets"))
            self.assertFalse(reply["inThisCell"])
            self.assertIn("explore", reply["hint"])
            await h.text("game_stop")
        run(go())


if __name__ == "__main__":
    unittest.main()
