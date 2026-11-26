import json
import unittest

from skyrim_devtools.use.combat import decide
import test_use
from test_use import Harness, run

COMBAT_METHODS = ["Combat.state", "Combat.target", "Combat.face", "Combat.draw", "Combat.equip", "Combat.unequip",
                  "Combat.attack", "Combat.block", "Combat.cast", "Combat.drink", "Move.walk", "Player.equipped",
                  "Player.spells"]


def hostile(distance=100, attack="none", pct=100, chosen=False):
    return {"refID": "0001", "distance": distance, "attack": attack, "health": {"pct": pct}, "chosen": chosen}


def state(hostiles, health=100, stamina=100, dead=False):
    return {"player": {"dead": dead, "health": {"pct": health}, "stamina": {"pct": stamina}},
            "hostiles": hostiles, "hostileTotal": len(hostiles)}


class DecideTests(unittest.TestCase):
    def test_done_when_clear_or_dead(self):
        self.assertEqual(decide(state([])), ("done", "clear"))
        self.assertEqual(decide(state([hostile()], dead=True)), ("done", "dead"))

    def test_drink_below_threshold_only_with_potion(self):
        self.assertEqual(decide(state([hostile()], health=20)), ("drink", "health"))
        self.assertEqual(decide(state([hostile()], health=20), have_potion=False)[0], "attack")

    def test_approach_then_attack(self):
        self.assertEqual(decide(state([hostile(distance=600)])), ("approach", 600))
        self.assertEqual(decide(state([hostile(distance=120)])), ("attack", "power"))

    def test_low_stamina_uses_light_attacks(self):
        self.assertEqual(decide(state([hostile()], stamina=30)), ("attack", "light"))

    def test_block_an_incoming_swing(self):
        self.assertEqual(decide(state([hostile(attack="windup")])), ("block", None))
        self.assertEqual(decide(state([hostile(attack="windup")]), block_incoming=False)[0], "attack")
        self.assertEqual(decide(state([hostile(attack="swing")], stamina=5))[0], "attack")

    def test_mage_casts_from_range(self):
        self.assertEqual(decide(state([hostile(distance=900)]), style="mage"), ("cast", None))
        self.assertEqual(decide(state([hostile(distance=2000)]), style="mage")[0], "approach")

    def test_chosen_target_beats_nearest(self):
        far = hostile(distance=500, chosen=True)
        self.assertEqual(decide(state([hostile(distance=100), far])), ("approach", 500))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.saved = list(test_use.METHODS)
        test_use.METHODS.extend(COMBAT_METHODS)

    def tearDown(self):
        test_use.METHODS[:] = self.saved

    def test_tools_forward_only_set_parameters(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            await h.text("combat_equip", name="Iron Sword", hand="left")
            await h.text("combat_attack", kind="power")
            await h.text("combat_target")
            await h.text("combat_drink", kind="health")
            await h.text("combat_state", radius=800)
            calls = [c for c in h.sockets[0].calls if c[0].startswith("Combat.")]
            self.assertEqual(calls, [
                ("Combat.equip", {"name": "Iron Sword", "hand": "left"}),
                ("Combat.attack", {"kind": "power", "hand": "right", "face": True}),
                ("Combat.target", {"face": True}),
                ("Combat.drink", {"kind": "health"}),
                ("Combat.state", {"radius": 800}),
            ])
            await h.text("game_stop")
        run(scenario())

    def test_fight_loop_stops_when_clear(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.menus = set()
            player = {"dead": False, "health": {"cur": 100, "max": 100, "pct": 100}, "stamina": {"pct": 100}}
            socket.results["Combat.state"] = {"player": player, "hostileTotal": 0, "hostiles": []}
            result = json.loads(await h.text("combat_fight"))
            self.assertEqual(result["outcome"], "clear")
            self.assertEqual(result["hostilesLeft"], 0)
            await h.text("game_stop")
        run(scenario())

    def test_fight_loop_closes_a_tutorial_popup_and_stops_on_other_menus(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            player = {"dead": False, "health": {"cur": 100, "max": 100, "pct": 100}, "stamina": {"pct": 100}}
            socket.results["Combat.state"] = {"player": player, "hostileTotal": 0, "hostiles": []}
            socket.results["UI.listMenus"] = [{"name": "Tutorial Menu", "open": True}]
            result = json.loads(await h.text("combat_fight", rounds=3))
            self.assertIn(("UI.closeMenu", {"name": "Tutorial Menu", "wait": True}), [(m, p) for m, p in socket.calls])
            socket.results["UI.listMenus"] = [{"name": "InventoryMenu", "open": True}]
            result = json.loads(await h.text("combat_fight", rounds=3))
            self.assertEqual(result["outcome"], "menu open: InventoryMenu")
            await h.text("game_stop")
        run(scenario())

    def test_missing_plugin_method_is_explained(self):
        async def scenario():
            test_use.METHODS[:] = self.saved
            h = Harness()
            await h.text("game_start")
            with self.assertRaises(Exception) as caught:
                await h.text("combat_state")
            self.assertIn("not available in this plugin build", str(caught.exception))
            await h.text("game_stop")
        run(scenario())


if __name__ == "__main__":
    unittest.main()
