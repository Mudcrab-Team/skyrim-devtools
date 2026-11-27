import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("audio_soak", Path(__file__).parent / "live" / "audio_soak.py")
soak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soak)


def series(threads_at, minutes=45, **extra):
    return [{"t": m * 60, "alive": True, "threads": threads_at(m), "responding": True, **extra} for m in range(minutes + 1)]


class EvaluateTests(unittest.TestCase):
    def test_flat_series_passes(self):
        ok, checks = soak.evaluate(series(lambda m: 140 + (m % 3)), 45)
        self.assertTrue(ok, checks)

    def test_leak_of_two_per_second_fails(self):
        ok, checks = soak.evaluate(series(lambda m: 135 + 120 * m), 45)
        self.assertFalse(ok)
        self.assertGreater(checks["growthPerMinLast30"], 100)

    def test_slow_growth_under_cap_passes_and_over_cap_fails(self):
        self.assertTrue(soak.evaluate(series(lambda m: 135 + 5 * m), 45)[0])
        self.assertFalse(soak.evaluate(series(lambda m: 135 + 11 * m), 45)[0])

    def test_hang_or_vanish_fails(self):
        samples = series(lambda m: 140)
        samples[20]["responding"] = False
        self.assertFalse(soak.evaluate(samples, 45)[0])
        samples = series(lambda m: 140)
        samples[30] = {"t": 1800, "alive": False}
        self.assertFalse(soak.evaluate(samples, 45)[0])

    def test_short_run_cannot_pass(self):
        self.assertFalse(soak.evaluate(series(lambda m: 140, minutes=3), 45)[0])

    def test_slope(self):
        self.assertAlmostEqual(soak.slope_per_min([(0, 0), (1, 2), (2, 4)]), 2.0)
        self.assertEqual(soak.slope_per_min([(1, 5)]), 0.0)


if __name__ == "__main__":
    unittest.main()
