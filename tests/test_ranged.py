import asyncio
import math
import unittest

from skyrim_devtools.use import ranged, ranged_math as rm


def fly(speed, gravity, pitch_deg, heading_deg=0.0, dt=0.02, steps=300, origin=(0.0, 0.0, 0.0)):
    """Reference flight: the path of a projectile under constant gravity."""
    pitch, heading = math.radians(pitch_deg), math.radians(heading_deg)
    v = [speed * math.cos(pitch) * math.sin(heading), speed * math.cos(pitch) * math.cos(heading), speed * math.sin(pitch)]
    p = list(origin)
    out = []
    t = 0.0
    for _ in range(steps):
        out.append((t, tuple(p), tuple(v)))
        for i in range(3):
            p[i] += v[i] * dt
        v[2] -= gravity * dt
        t += dt
    return out


class BallisticTests(unittest.TestCase):
    def test_flat_shot_without_gravity_points_straight(self):
        pitch, tof, ok = rm.solve_pitch(1000, 0, 3000, 0)
        self.assertTrue(ok)
        self.assertAlmostEqual(pitch, 0.0, places=6)
        self.assertAlmostEqual(tof, 1000 / 3000, places=6)

    def test_solution_lands_on_target(self):
        for flat, rise in ((800, 0), (1500, 200), (1200, -400), (600, 350)):
            pitch, tof, ok = rm.solve_pitch(flat, rise, 3000, 686.7 * 0.69)
            self.assertTrue(ok)
            path = fly(3000, 686.7 * 0.69, pitch)
            reach = rm.miss_at_range([s[1] for s in path], (0, 0, 0), (0, flat, rise))
            self.assertIsNotNone(reach)
            self.assertLess(abs(reach[0]), 5.0, (flat, rise))
            self.assertAlmostEqual(tof, next(s[0] for s in path if s[1][1] >= flat), delta=0.03)

    def test_drop_needs_positive_pitch_over_distance(self):
        near = rm.solve_pitch(300, 0, 3000, 500)[0]
        far = rm.solve_pitch(1500, 0, 3000, 500)[0]
        self.assertGreater(far, near)
        self.assertGreater(near, 0)

    def test_out_of_range_reports_unreachable(self):
        pitch, _, ok = rm.solve_pitch(50000, 0, 1000, 700)
        self.assertFalse(ok)
        self.assertEqual(pitch, 45.0)

    def test_heading_is_clockwise_from_north(self):
        self.assertAlmostEqual(rm.heading_deg((0, 0, 0), (0, 10, 0)), 0.0)
        self.assertAlmostEqual(rm.heading_deg((0, 0, 0), (10, 0, 0)), 90.0)
        self.assertAlmostEqual(rm.heading_deg((0, 0, 0), (0, -10, 0)), 180.0)
        self.assertAlmostEqual(rm.heading_deg((0, 0, 0), (-10, 0, 0)), 270.0)

    def test_lead_hits_a_moving_target(self):
        origin, start, vel = (0, 0, 0), (900.0, 900.0, 100.0), (-120.0, 60.0, 0.0)
        aim = rm.aim_at(origin, start, 3000, 400, vel, latency=0.0)
        path = fly(3000, 400, aim["pitch"], aim["heading"], dt=0.005, steps=800)
        t_hit = aim["time"]
        later = tuple(start[i] + vel[i] * t_hit for i in range(3))
        near = min(path, key=lambda s: abs(s[0] - t_hit))
        self.assertLess(math.dist(near[1], later), 12.0)
        still = rm.aim_at(origin, start, 3000, 400)
        self.assertGreater(abs(rm.wrap180(aim["heading"] - still["heading"])), 0.5)

    def test_velocity_estimate(self):
        samples = [(0.0, (0, 0, 0)), (0.5, (50, 0, 10)), (1.0, (100, 0, 20))]
        self.assertEqual(rm.estimate_velocity(samples), (100.0, 0.0, 20.0))
        self.assertEqual(rm.estimate_velocity([(0, (1, 1, 1))]), (0.0, 0.0, 0.0))


class CalibrationTests(unittest.TestCase):
    def test_fit_recovers_speed_gravity_elevation(self):
        flight = fly(2400, 500, 7.5, 33.0)
        fit = rm.fit_flight(flight[:40])
        self.assertAlmostEqual(fit["speed"], 2400, delta=1)
        self.assertAlmostEqual(fit["gravity"], 500, delta=10)
        self.assertAlmostEqual(fit["elevation"], 7.5, delta=0.01)

    def test_fit_needs_motion(self):
        self.assertEqual(rm.fit_flight([(0, (0, 0, 0), (0, 0, 0))])["speed"], None)

    def test_miss_error_sign_and_trim_direction(self):
        # shoot 2 degrees too low: the arrow passes under the aim point
        pitch, _, _ = rm.solve_pitch(1000, 0, 3000, 500)
        path = [s[1] for s in fly(3000, 500, pitch - 2.0)]
        vertical, lateral = rm.miss_at_range(path, (0, 0, 0), (0, 1000, 0))
        self.assertLess(vertical, -20)
        self.assertAlmostEqual(lateral, 0.0, delta=1.0)
        dp, dh = rm.trim_for_miss(vertical, lateral, 1000, gain=1.0)
        self.assertGreater(dp, 1.0)
        self.assertAlmostEqual(dh, 0.0, delta=0.1)

    def test_lateral_miss_to_the_right_turns_left(self):
        path = [s[1] for s in fly(3000, 0, 0.0, heading_deg=3.0)]
        vertical, lateral = rm.miss_at_range(path, (0, 0, 0), (0, 1000, 0))
        self.assertGreater(lateral, 20)
        self.assertLess(rm.trim_for_miss(vertical, lateral, 1000, gain=1.0)[1], -1.0)

    def test_short_flight_has_no_miss_reading(self):
        path = [s[1] for s in fly(3000, 500, 0.0)[:5]]
        self.assertIsNone(rm.miss_at_range(path, (0, 0, 0), (0, 1000, 0)))

    def test_default_draw_is_a_full_draw(self):
        self.assertGreaterEqual(rm.default_draw_s(1.0), 1.5)
        self.assertGreaterEqual(rm.default_draw_s(None), 1.5)
        self.assertGreater(rm.default_draw_s(0.5), rm.default_draw_s(1.0))


class SamplingTests(unittest.TestCase):
    def test_samples_stay_within_radius_and_are_ranked(self):
        player, target = (0.0, 0.0, 0.0), (0.0, 2000.0, 400.0)
        out = rm.sample_positions(player, target)
        self.assertEqual(len(out), 64)
        for c in out:
            self.assertLessEqual(math.hypot(c["pos"][0], c["pos"][1]), 600.0 + 1e-6)
        self.assertEqual([c["score"] for c in out], sorted(c["score"] for c in out))

    def test_prefers_the_range_band(self):
        out = rm.sample_positions((0.0, 0.0, 0.0), (0.0, 1800.0, 0.0))
        best = out[0]
        self.assertLessEqual(best["range"], rm.MAX_RANGE + 1)
        self.assertGreater(best["pos"][1], 0.0)

    def test_close_target_picks_a_point_that_backs_away(self):
        out = rm.sample_positions((0.0, 0.0, 0.0), (0.0, 300.0, 0.0))
        self.assertLess(out[0]["pos"][1], 0.0)
        self.assertGreaterEqual(out[0]["range"], 590.0)


class FakeNative:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def __call__(self, method, **params):
        self.calls.append((method, params))
        value = self.replies.get(method)
        if callable(value):
            return value(params)
        return value


class FightStepTests(unittest.TestCase):
    def state(self, dz=300):
        return {"player": {"rightHand": {"name": "Hunting Bow"}},
                "hostiles": [{"refID": "00AA", "distance": 900, "dz": dz, "chosen": True}]}

    def test_plan_classification(self):
        self.assertEqual(ranged.melee_unreachable({"found": True, "complete": True}, 400), (False, ""))
        unreachable, reason = ranged.melee_unreachable({"found": True, "complete": False}, 400)
        self.assertTrue(unreachable)
        self.assertIn("higher", reason)
        self.assertEqual(ranged.melee_unreachable({"found": False}, 0), (False, ""))

    def test_reachable_target_stays_melee(self):
        native = FakeNative({"Nav.plan": {"found": True, "complete": True}})
        step = asyncio.run(ranged.fight_step(native, None, self.state(), {}, 1))
        self.assertIsNone(step)

    def test_unreachable_without_bow_reports_unreachable(self):
        native = FakeNative({"Nav.plan": {"found": True, "complete": False},
                             "Ranged.loadout": {"ready": False, "missing": ["bow", "arrows"]}})
        step = asyncio.run(ranged.fight_step(native, None, self.state(), {}, 1))
        self.assertEqual(step["log"], "unreachable")
        self.assertTrue(step["stop"])
        self.assertIn("bow", step["advice"])

    def test_reachability_is_cached_between_rounds(self):
        native = FakeNative({"Nav.plan": {"found": True, "complete": True}})
        memory = {}
        for number in (1, 2, 3):
            asyncio.run(ranged.fight_step(native, None, self.state(), memory, number))
        self.assertEqual(sum(1 for c in native.calls if c[0] == "Nav.plan"), 1)

    def test_resolve_target_by_name_and_nearest(self):
        state = {"hostiles": [{"refID": "00AA", "name": "Bandit Archer"}, {"refID": "00BB", "name": "Wolf", "chosen": True}]}
        native = FakeNative({"Combat.state": state})
        self.assertEqual(asyncio.run(ranged.resolve_target(native, "archer"))[:2], ("00AA", "Bandit Archer"))
        self.assertEqual(asyncio.run(ranged.resolve_target(native, None))[:2], ("00BB", "Wolf"))
        self.assertEqual(asyncio.run(ranged.resolve_target(native, "0x14F2A0"))[0], "0x14F2A0")
        self.assertEqual(asyncio.run(ranged.resolve_target(native, "dragon"))[0], None)


class ShooterTests(unittest.TestCase):
    def test_missing_bow_is_reported(self):
        native = FakeNative({"Ranged.loadout": {"ready": False, "missing": ["bow"]}})
        shooter = ranged.Shooter(native, None, "00AA")
        result = asyncio.run(shooter.run(1, False, 0))
        self.assertEqual(result["error"], "missing")
        self.assertEqual(result["missing"], ["bow"])

    def test_blocked_line_suggests_a_reachable_position(self):
        def los(params):
            if "points" in params:
                return {"clear": False, "points": [{"clear": i % 5 == 0} for i in range(len(params["points"]))]}
            return {"clear": False, "target": [0, 1200, 600], "from": [0, 0, 110], "hitDistance": 300, "hitLayer": 1}

        native = FakeNative({
            "Ranged.loadout": {"ready": True, "equipQueued": False, "bowEquipped": True, "ammoEquipped": True,
                               "bow": {"speed": 1.0}, "ammo": {"projectile": {"speed": 3000, "gravity": 0.69}}},
            "Ranged.state": {"drawn": True, "position": [0, 0, 0]},
            "Ranged.los": los,
            "Nav.plan": lambda p: {"found": True, "complete": True, "points": [[0, 0, 0], [p["x"], p["y"], p["z"]]]},
        })
        shooter = ranged.Shooter(native, None, "00AA")
        result = asyncio.run(shooter.run(1, False, 0))
        self.assertEqual(result["error"], "noLineOfSight")
        suggestion = result["suggestedPosition"]
        self.assertIn("x", suggestion)
        self.assertGreater(suggestion["withLineOfSight"], 0)
        self.assertIn("hitDistance", result["blocker"])
        self.assertLessEqual(math.hypot(suggestion["x"], suggestion["y"]), 600.1)


if __name__ == "__main__":
    unittest.main()
