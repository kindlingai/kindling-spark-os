#!/usr/bin/env python3
"""sparkos-thermal's decision rule, without a GPU: python3 tools/test-sparkos-thermal.py"""
import importlib.machinery
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
T = importlib.machinery.SourceFileLoader("thermal", os.path.join(HERE, "../overlay/usr/local/sbin/sparkos-thermal")).load_module()
C = dict(T.DEFAULTS)


class Decide(unittest.TestCase):
    def test_steps_down_harder_the_hotter_it_is(self):
        self.assertEqual([T.decide(C, 2000, t, 90, None, 0)[0] for t in (80, 81, 82, 83, 84, 85, 86, 87, 95)],
                         [2000, 1900, 1900, 1800, 1800, 1700, 1700, 1500, 1500])
        self.assertEqual(T.decide(C, 1550, 84, 90, None, 0)[0], 1500)        # never below the floor

    def test_holds_between_optimal_and_target_and_while_idle(self):
        self.assertEqual(T.decide(C, 2000, 78, 90, 5.0, 100), (2000, None))   # warm: the cool timer restarts
        self.assertEqual(T.decide(C, 2000, 50, 0, 5.0, 100), (2000, None))    # idle: no raise however cool

    def test_raises_after_a_minute_cool_and_busy_then_waits_again(self):
        cap, since = T.decide(C, 2000, 70, 90, None, 0)
        self.assertEqual((cap, since), (2000, 0))
        self.assertEqual(T.decide(C, cap, 70, 90, since, 59), (2000, 0))
        cap, since = T.decide(C, cap, 70, 90, since, 60)
        self.assertEqual((cap, since), (2100, 60))
        self.assertEqual(T.decide(C, 2400, 70, 90, 0, 60), (2400, 0))         # never above the ceiling

    def test_hot_acpi_zone_steps_down_and_blocks_raises(self):
        self.assertEqual([T.decide(C, 2000, 70, 90, None, 0, z)[0] for z in (85, 89, 90, 91, 92, 95, 99)],
                         [2000, 2000, 1900, 1900, 1800, 1700, 1700])
        self.assertEqual(T.decide(C, 2000, 70, 90, 0, 60, 86), (2000, None))   # within 5 C of the limit: no raise
        self.assertEqual(T.decide(C, 2000, 70, 90, 0, 60, 85), (2100, 60))
        self.assertEqual(T.decide(C, 2000, 88, 90, None, 0, 80)[0], 1500)     # the GPU rule still wins

    def test_hottest_zone_reads_acpitz_only(self):
        with tempfile.TemporaryDirectory() as d:
            for name, kind, milli in (("thermal_zone0", "acpitz", "85300"), ("thermal_zone1", "acpitz", "71000"),
                                      ("thermal_zone2", "cpu-thermal", "99000"), ("cooling_device0", "acpitz", "1")):
                os.mkdir(f"{d}/{name}")
                open(f"{d}/{name}/type", "w").write(kind + "\n")
                open(f"{d}/{name}/temp", "w").write(milli + "\n")
            self.assertEqual(T.hottest_zone(d), 85)
            self.assertIsNone(T.hottest_zone(f"{d}/missing"))

    def test_config_overrides_and_refuses_an_optimal_at_or_above_target(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("# hotter box\nTHERMAL_TARGET=78\nTHERMAL_OPTIMAL='72'\nOTHER=1\n")
        try:
            c = T.config(f.name)
            self.assertEqual((c["THERMAL_TARGET"], c["THERMAL_OPTIMAL"], c["THERMAL_INTERVAL"]), (78, 72, 15))
            with open(f.name, "w") as g:
                g.write("THERMAL_OPTIMAL=80\n")
            with self.assertRaises(SystemExit):
                T.config(f.name)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
