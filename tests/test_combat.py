import json
import unittest

from skyrim_devtools.use.combat import assess, decide, healing_potions, healing_spells
import test_use
from test_use import Harness, run

COMBAT_METHODS = ["Combat.state", "Combat.target", "Combat.face", "Combat.draw", "Combat.equip", "Combat.unequip",
                  "Combat.attack", "Combat.block", "Combat.cast", "Combat.drink", "Move.walk", "Player.equipped", "Quest.targets", "Nav.goto",
                  "Player.spells", "Player.inventory"]


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


class HealAndAssessTests(unittest.TestCase):
    def test_heal_by_spell_without_potion_then_retreat(self):
        low = state([hostile()], health=20)
        self.assertEqual(decide(low, have_potion=False, have_heal_spell=True), ("heal", "spell"))
        self.assertEqual(decide(low, have_potion=False, retreat_below=25), ("retreat", None))
        self.assertEqual(decide(low, have_potion=False, retreat_below=10)[0], "attack")
        self.assertEqual(decide(low, have_heal_spell=True), ("drink", "health"))

    def test_healing_spells_exclude_other_target_ones(self):
        spells = {"spells": [{"name": "Healing"}, {"name": "Healing Hands"}, {"name": "Flames"}, {"name": "Close Wounds"}]}
        self.assertEqual(healing_spells(spells), ["Healing", "Close Wounds"])

    def test_assess_by_level_and_trades(self):
        calm = state([dict(hostile(distance=300), level=2)])
        calm["player"]["level"] = 5
        self.assertEqual(assess(calm), (False, ""))
        pack = state([dict(hostile(distance=300), level=9), dict(hostile(distance=400), level=9)])
        pack["player"]["level"] = 1
        self.assertTrue(assess(pack)[0])
        boss = state([dict(hostile(distance=300), level=30)])
        boss["player"]["level"] = 20
        self.assertIn("level 30", assess(boss, ())[1]) if assess(boss)[0] else None
        trades = [(100, 100), (80, 95), (60, 92), (45, 90), (40, 88)]
        self.assertTrue(assess(calm, trades)[0])
        self.assertFalse(assess(calm, [(100, 100), (90, 70), (80, 50), (75, 30), (70, 10)])[0])

    def test_unknown_player_level_skips_the_level_rule(self):
        self.assertEqual(assess(state([dict(hostile(), level=50)])), (False, ""))


class DaemonTests(unittest.TestCase):
    def test_daemon_is_stale_only_when_sources_changed_after_start(self):
        from skyrim_devtools.use import server
        saved = server._STARTED
        try:
            server._STARTED = server.source_mtime() + 100
            self.assertFalse(server.daemon_state()["stale"])
            server._STARTED = server.source_mtime() - 100
            self.assertTrue(server.daemon_state()["stale"])
        finally:
            server._STARTED = saved

    def test_busy_error_recognises_a_load(self):
        from skyrim_devtools.use.manager import GameError
        from skyrim_devtools.use.server import is_busy_error
        self.assertTrue(is_busy_error(GameError("x (the game did not answer in time (it may be loading or stalled))")))
        self.assertFalse(is_busy_error(GameError("not found")))


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
            self.assertIn(("UI.closeMenu", {"name": "Tutorial Menu"}), [(m, p) for m, p in socket.calls])
            socket.results["UI.listMenus"] = [{"name": "InventoryMenu", "open": True}]
            result = json.loads(await h.text("combat_fight", rounds=3))
            self.assertEqual(result["outcome"], "menu open: InventoryMenu")
            await h.text("game_stop")
        run(scenario())

    def test_only_menus_that_pause_or_take_input_block_play(self):
        from skyrim_devtools.use.combat import blocks_play
        hud_widget = {"name": "TrueHUD", "open": True, "pausesGame": False, "usesMenuContext": False}
        self.assertFalse(blocks_play(hud_widget))
        self.assertTrue(blocks_play({"name": "InventoryMenu", "open": True, "pausesGame": True, "usesMenuContext": True}))
        self.assertTrue(blocks_play({"name": "Mod Dialog", "open": True, "pausesGame": False, "usesMenuContext": True}))
        self.assertFalse(blocks_play({"name": "InventoryMenu", "open": False, "pausesGame": True}))
        # an older plugin sends no flags
        self.assertFalse(blocks_play({"name": "HUD Menu", "open": True}))
        self.assertTrue(blocks_play({"name": "InventoryMenu", "open": True}))

    def test_quest_targets_forwards_only_set_parameters(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            await h.text("quest_targets", quest="MS13")
            await h.text("nav_goto", goal={"questTarget": "MS13"}, activate=True)
            calls = [c for c in h.sockets[0].calls if c[0] in ("Quest.targets", "Nav.goto")]
            self.assertEqual(calls[0], ("Quest.targets", {"quest": "MS13"}))
            self.assertEqual(calls[1][1]["activate"], True)
            self.assertEqual(calls[1][1]["goal"], {"questTarget": "MS13"})
            await h.text("game_stop")
        run(scenario())

    def test_fight_reports_outmatched_before_fighting(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.menus = set()
            player = {"dead": False, "level": 1, "health": {"cur": 100, "max": 100, "pct": 100}, "stamina": {"pct": 100}}
            pack = [dict(hostile(distance=300), level=9) for _ in range(2)]
            socket.results["Combat.state"] = {"player": player, "hostileTotal": 2, "hostiles": pack}
            socket.results["Player.spells"] = {"spells": []}
            result = json.loads(await h.text("combat_fight", rounds=3))
            self.assertEqual(result["outcome"], "outmatched")
            self.assertIn("enemy strength", result["assessment"])
            self.assertFalse([c for c in socket.calls if c[0] == "Combat.attack"])
            await h.text("game_stop")
        run(scenario())

    def test_fight_warns_at_the_start_when_nothing_can_heal(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.menus = set()
            player = {"dead": False, "health": {"cur": 100, "max": 100, "pct": 100}, "stamina": {"pct": 100}}
            socket.results["Combat.state"] = {"player": player, "hostileTotal": 0, "hostiles": []}
            socket.results["Player.spells"] = {"spells": []}
            socket.results["Player.inventory"] = {"total": 1, "items": [{"name": "Iron Dagger", "type": "WEAP", "count": 1}]}
            result = json.loads(await h.text("combat_fight"))
            self.assertIn("no healing potion", result["warnings"][0])
            self.assertEqual(result["healingPotions"], 0)
            events, _, _ = h.manager.drain_events(types={"warning"})
            self.assertEqual(events[0]["data"]["code"], "noHealingPotion")
            socket.results["Player.inventory"] = {"total": 1, "items": [
                {"name": "Potion of Minor Healing", "type": "ALCH", "count": 3}, {"name": "Poison", "count": 9}]}
            fine = json.loads(await h.text("combat_fight"))
            self.assertNotIn("warnings", fine)
            self.assertEqual(fine["healingPotions"], 3)
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
