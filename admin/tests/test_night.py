import unittest
from datetime import datetime
from datetime import time as dtime

from modules import night

LATE = {"night": {"bedtime": "23:30", "wake": "06:30"}}
EARLY = {"night": {"bedtime": "01:00", "wake": "07:00"}}


def ts(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s).timestamp()


class AcrossMidnight(unittest.TestCase):
    def test_window(self):
        self.assertEqual(night.window(LATE), (dtime(23, 30), dtime(6, 30)))

    def test_quiet(self):
        for at, want in [((2026, 10, 10, 23, 29, 59), False), ((2026, 10, 10, 23, 30), True),
                         ((2026, 10, 11, 0, 0), True), ((2026, 10, 11, 3, 0), True),
                         ((2026, 10, 11, 6, 29, 59), True), ((2026, 10, 11, 6, 30), False),
                         ((2026, 10, 11, 12, 0), False)]:
            self.assertIs(night.quiet(LATE, ts(*at)), want, at)

    def test_night_of_edges(self):
        self.assertEqual(night.night_of(ts(2026, 10, 10, 23, 30), LATE), "2026-10-10")
        self.assertEqual(night.night_of(ts(2026, 10, 11, 0, 0), LATE), "2026-10-10")
        self.assertEqual(night.night_of(ts(2026, 10, 11, 6, 29, 59), LATE), "2026-10-10")
        self.assertIsNone(night.night_of(ts(2026, 10, 11, 6, 30), LATE))
        self.assertIsNone(night.night_of(ts(2026, 10, 10, 23, 29, 59), LATE))

    def test_bounds(self):
        self.assertEqual(night.bounds("2026-10-10", LATE), (ts(2026, 10, 10, 23, 30), ts(2026, 10, 11, 6, 30)))
        self.assertEqual(night.bounds(datetime(2026, 10, 10).date(), LATE), night.bounds("2026-10-10", LATE))

    def test_next_wake(self):
        self.assertEqual(night.next_wake(LATE, ts(2026, 10, 10, 22, 0)), ts(2026, 10, 11, 6, 30))
        self.assertEqual(night.next_wake(LATE, ts(2026, 10, 11, 3, 0)), ts(2026, 10, 11, 6, 30))
        self.assertEqual(night.next_wake(LATE, ts(2026, 10, 11, 6, 30)), ts(2026, 10, 12, 6, 30))


class AfterMidnight(unittest.TestCase):
    def test_quiet(self):
        for at, want in [((2026, 10, 11, 0, 59, 59), False), ((2026, 10, 11, 1, 0), True),
                         ((2026, 10, 11, 6, 59, 59), True), ((2026, 10, 11, 7, 0), False),
                         ((2026, 10, 10, 23, 59), False)]:
            self.assertIs(night.quiet(EARLY, ts(*at)), want, at)

    def test_night_of_edges(self):
        # A bedtime before noon belongs to the evening before.
        self.assertEqual(night.night_of(ts(2026, 10, 11, 1, 0), EARLY), "2026-10-10")
        self.assertEqual(night.night_of(ts(2026, 10, 11, 6, 59, 59), EARLY), "2026-10-10")
        self.assertIsNone(night.night_of(ts(2026, 10, 11, 7, 0), EARLY))
        self.assertIsNone(night.night_of(ts(2026, 10, 11, 0, 59, 59), EARLY))

    def test_bounds(self):
        self.assertEqual(night.bounds("2026-10-10", EARLY), (ts(2026, 10, 11, 1, 0), ts(2026, 10, 11, 7, 0)))

    def test_next_wake(self):
        self.assertEqual(night.next_wake(EARLY, ts(2026, 10, 11, 7, 0)), ts(2026, 10, 12, 7, 0))


class NoNight(unittest.TestCase):
    def test_without_config(self):
        now = ts(2026, 10, 11, 3, 0)
        for cfg in (None, {}, {"night": {}}, {"night": {"bedtime": "25:00", "wake": "06:30"}},
                    {"night": {"bedtime": "late", "wake": "06:30"}}, {"night": {"bedtime": "06:30", "wake": "06:30"}}):
            self.assertIs(night.quiet(cfg, now), False, cfg)
            self.assertIsNone(night.window(cfg), cfg)
            self.assertIsNone(night.night_of(now, cfg), cfg)
            self.assertIsNone(night.bounds("2026-10-10", cfg), cfg)
            self.assertIsNone(night.next_wake(cfg, now), cfg)

    def test_quiet_defaults_to_now(self):
        self.assertIs(night.quiet({}), False)
        self.assertIsInstance(night.quiet(LATE), bool)


if __name__ == "__main__":
    unittest.main()
