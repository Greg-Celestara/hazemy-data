"""MYT day boundaries, 3-hourly windows and the vector-mean wind."""
import unittest
from datetime import datetime, timezone

from hazemy import compute
from hazemy.common import myt_day_bounds, steps_between


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class MytDays(unittest.TestCase):
    def test_1600_utc_is_midnight_myt(self):
        start, end = myt_day_bounds(utc(2026, 10, 19, 16, 0))
        self.assertEqual((start, end), (utc(2026, 10, 19, 16), utc(2026, 10, 20, 16)))

    def test_1559_utc_still_belongs_to_the_earlier_myt_day(self):
        start, _ = myt_day_bounds(utc(2026, 10, 19, 15, 59))
        self.assertEqual(start, utc(2026, 10, 18, 16))

    def test_tomorrow_offset(self):
        start, end = myt_day_bounds(utc(2026, 10, 19, 10, 20), 1)
        self.assertEqual((start, end), (utc(2026, 10, 19, 16), utc(2026, 10, 20, 16)))

    def test_a_myt_day_has_eight_three_hourly_steps(self):
        steps = steps_between(*myt_day_bounds(utc(2026, 10, 19, 10, 0)))
        self.assertEqual(len(steps), 8)
        self.assertEqual(steps[0], utc(2026, 10, 18, 18))
        self.assertEqual(steps[-1], utc(2026, 10, 19, 15))

    def test_today_and_tomorrow_do_not_overlap(self):
        now = utc(2026, 10, 19, 10, 0)
        self.assertFalse(set(compute.today_steps(now)) & set(compute.tomorrow_steps(now)))

    def test_required_times_start_21h_before_the_first_window_end(self):
        needed = compute.required_times(utc(2026, 10, 19, 10, 20))
        self.assertEqual(needed[0], utc(2026, 10, 18, 12))   # now-window: 09:00 UTC minus 21 h
        self.assertEqual(needed[-1], utc(2026, 10, 20, 15))  # last step of tomorrow MYT


class WindVector(unittest.TestCase):
    def test_opposing_winds_cancel(self):
        wind = compute.wind_vector([5.0, -5.0], [0.0, 0.0])
        self.assertEqual(wind["speed_kmh"], 0)

    def test_westerly_blows_from_west(self):
        wind = compute.wind_vector([10.0], [0.0])  # u>0: air moves toward the east
        self.assertEqual((wind["from_deg"], wind["from_compass"], wind["speed_kmh"]),
                         (270, "W", 36))

    def test_southwesterly(self):
        wind = compute.wind_vector([3.0], [3.0])  # moving toward the north-east
        self.assertEqual((wind["from_deg"], wind["from_compass"]), (225, "SW"))

    def test_northerly_wraps_to_zero_not_360(self):
        wind = compute.wind_vector([0.0], [-4.0])
        self.assertEqual((wind["from_deg"], wind["from_compass"]), (0, "N"))

    def test_vector_mean_is_not_the_mean_of_speeds(self):
        wind = compute.wind_vector([10.0, 0.0], [0.0, 10.0])  # from W then from S
        self.assertEqual(wind["from_compass"], "SW")
        self.assertEqual(wind["speed_kmh"], 25)  # 7.07 m/s * 3.6


if __name__ == "__main__":
    unittest.main()
