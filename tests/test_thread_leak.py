import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("thread_leak", Path(__file__).parent / "live" / "thread_leak.py")
thread_leak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(thread_leak)


def census(total, groups):
    return {"alive": True, "total": total, "groups": [
        {"start": s, "state": "Waiting", "waitReason": 6, "count": n,
         "signatures": [{"count": n, "frames": f} for f in sigs]} for s, n, sigs in groups]}


class ThreadLeakTests(unittest.TestCase):
    def test_slope(self):
        self.assertAlmostEqual(thread_leak.slope_per_min([(0, 10), (1, 20), (2, 30)]), 10.0)
        self.assertEqual(thread_leak.slope_per_min([(0, 5)]), 0.0)

    def test_growing_group_is_ranked_first(self):
        samples = [(0, census(30, [("a.dll+0x1", 10, []), ("b.dll+0x2", 20, [])])),
                   (60, census(45, [("a.dll+0x1", 10, []), ("b.dll+0x2", 35, [])])),
                   (120, census(60, [("a.dll+0x1", 10, []), ("b.dll+0x2", 50, [])]))]
        rows = thread_leak.growth_by_group(samples)
        self.assertEqual(rows[0]["start"], "b.dll+0x2")
        self.assertAlmostEqual(rows[0]["perMin"], 15.0)
        self.assertEqual(len(rows), 1)

    def test_signature_growth_names_the_owner(self):
        first = census(30, [("x", 30, ["p.dll+0x10 < q.dll+0x20", "s.exe+0x5"])])
        last = census(60, [("x", 60, ["p.dll+0x10 < q.dll+0x20", "s.exe+0x5"])])
        last["groups"][0]["signatures"] = [{"count": 55, "frames": "p.dll+0x10 < q.dll+0x20"}, {"count": 5, "frames": "s.exe+0x5"}]
        first["groups"][0]["signatures"] = [{"count": 25, "frames": "p.dll+0x10 < q.dll+0x20"}, {"count": 5, "frames": "s.exe+0x5"}]
        rows = thread_leak.signature_growth(first, last)
        self.assertEqual(rows, [{"frames": "p.dll+0x10 < q.dll+0x20", "first": 25, "last": 55, "delta": 30}])

    def test_evaluate_gate(self):
        samples = [(0, {"alive": True, "total": 100}), (600, {"alive": True, "total": 200})]
        self.assertTrue(thread_leak.evaluate(samples)[0])
        ok, checks = thread_leak.evaluate(samples, max_growth=1.0)
        self.assertFalse(ok)
        self.assertEqual(checks["growthPerMin"], 10.0)
        self.assertFalse(thread_leak.evaluate([(0, {"alive": None})])[0])


if __name__ == "__main__":
    unittest.main()
