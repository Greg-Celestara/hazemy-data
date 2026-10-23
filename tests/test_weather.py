"""GFS weather per cell: heat index, MYT day windows, missing steps, bundle decoding."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy import weather
from hazemy.common import Budget, PipelineError
from tests.gribmaker import make_message

CYCLE = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc)  # 18:30 MYT
LATS, LONS = np.array([3.0, 5.0]), np.array([101.0, 103.0])


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def wx_bundle(fhr: int, temp_k=303.15, rh=70.0, cumulative=None) -> bytes:
    """What the NOMADS filter returns for one hour: 2 m T, 2 m RH and the 0-N h rain."""
    def msg(value, param_id, **extra):
        return make_message(np.full((29, 57), value), lat_first=10.0, lon_first=94.0, di=0.5,
                            dj=0.5, param_id=param_id, step=fhr, **extra)
    out = msg(temp_k, 167, level_type=103, level=2) + msg(rh, 260242, level_type=103, level=2)
    if fhr > 0:
        out += msg(0.5 * fhr if cumulative is None else cumulative, 228)
    return out


def fields_with(temp_by_fhr: dict, acc_by_fhr: dict, rh=70.0) -> weather.WeatherFields:
    fields = weather.WeatherFields(CYCLE, 2)
    for fhr, value in temp_by_fhr.items():
        fields.temp[fhr] = np.full(2, float(value))
        fields.rh[fhr] = np.full(2, rh)
    for fhr, value in acc_by_fhr.items():
        fields.acc[fhr] = np.full(2, float(value))
    return fields


class HeatIndex(unittest.TestCase):
    def test_matches_the_noaa_chart_within_one_degree(self):
        # NWS heat index chart, deg F: (temperature, humidity) -> feels like
        for temp_f, rh, expected_f in ((90, 50, 95), (90, 70, 105), (100, 40, 109), (96, 50, 108)):
            got = float(weather.heat_index_c(f_to_c(temp_f), rh))
            self.assertAlmostEqual(got, f_to_c(expected_f), delta=1.0, msg=(temp_f, rh))

    def test_below_27_c_feels_like_is_the_temperature(self):
        self.assertEqual(float(weather.heat_index_c(26.9, 95)), 26.9)
        self.assertGreater(float(weather.heat_index_c(32.0, 70)), 38)

    def test_vectorised_over_cells(self):
        out = weather.heat_index_c(np.array([20.0, 32.0]), np.array([50.0, 70.0]))
        self.assertEqual(out[0], 20.0)
        self.assertGreater(out[1], 38)


class DayWindows(unittest.TestCase):
    def setUp(self):
        # Hours counted from the cycle: day +1 (20 Oct MYT) is 19 Oct 16:00 UTC to 20 Oct 16:00
        # UTC, whose 3-hourly steps are f018 ... f039. f015 belongs to today, f042 to day +2.
        temps = {fhr: 28.0 for fhr in range(0, 49, 3)}
        temps[15], temps[42] = 40.0, 10.0
        temps[27], temps[36] = 33.0, 25.0
        self.fields = fields_with(temps, {fhr: 0.5 * fhr for fhr in range(0, 49, 3)})

    def test_day_min_and_max_use_the_myt_day_only(self):
        day = weather.day_weather(self.fields, NOW, 1)[0]
        self.assertEqual((day["temp_min_c"], day["temp_max_c"]), (25.0, 33.0))

    def test_day_rain_runs_from_16_utc_to_16_utc(self):
        day = weather.day_weather(self.fields, NOW, 1)[0]
        self.assertEqual(day["rain_mm"], 12.0)  # 0.5 mm/h x 24 h

    def test_feels_like_max_is_the_highest_step(self):
        day = weather.day_weather(self.fields, NOW, 1)[0]
        self.assertEqual(day["feels_max_c"], int(round(float(weather.heat_index_c(33.0, 70.0)))))

    def test_today_covers_the_rest_of_the_day_from_the_nearest_step(self):
        today = weather.today_weather(self.fields, NOW)[0]
        # nearest step to 10:30 UTC is 09:00 (f009); steps 09, 12, 15 -> f015 is 40 C
        self.assertEqual(today["temp_max_c"], 40.0)
        self.assertEqual(today["temp_min_c"], 28.0)
        self.assertEqual(today["rain_mm"], 2.8)  # 10:30 to 16:00 UTC = 5.5 h at 0.5 mm/h

    def test_values_are_rounded_to_the_contract(self):
        self.fields.temp[18] = np.full(2, 29.449)
        self.fields.rh[18] = np.full(2, 70.0)
        day = weather.day_weather(self.fields, NOW, 1)[0]
        self.assertIsInstance(day["feels_max_c"], int)
        self.assertEqual(round(day["temp_max_c"], 1), day["temp_max_c"])


class MissingSteps(unittest.TestCase):
    def build(self):
        temps = {fhr: 30.0 for fhr in range(0, 49, 3)}
        return fields_with(temps, {fhr: 0.5 * fhr for fhr in range(0, 49, 3)})

    def test_a_missing_temperature_step_gives_null_temperatures_and_keeps_rain(self):
        fields = self.build()
        del fields.temp[30], fields.rh[30]
        day = weather.day_weather(fields, NOW, 1)[0]
        self.assertEqual((day["temp_min_c"], day["temp_max_c"], day["feels_max_c"]),
                         (None, None, None))
        self.assertEqual(day["rain_mm"], 12.0)

    def test_a_missing_rain_hour_gives_null_rain_and_keeps_temperatures(self):
        fields = self.build()
        del fields.acc[39]
        day = weather.day_weather(fields, NOW, 1)[0]
        self.assertIsNone(day["rain_mm"])
        self.assertEqual(day["temp_max_c"], 30.0)

    def test_now_weather_uses_the_nearest_step_and_the_3h_before_it(self):
        fields = self.build()
        fields.temp[9] = np.full(2, 31.26)
        fields.rh[9] = np.full(2, 74.0)
        fields.acc[6], fields.acc[9] = np.full(2, 2.0), np.full(2, 2.4)
        now = weather.now_weather(fields, NOW)[0]
        self.assertEqual((now["temp_c"], now["rh"], now["rain_mm_3h"]), (31.3, 74, 0.4))
        self.assertEqual(now["feels_c"], int(round(float(weather.heat_index_c(31.26, 74.0)))))

    def test_now_rain_is_null_when_the_earlier_step_is_missing(self):
        fields = self.build()
        del fields.acc[6]
        self.assertIsNone(weather.now_weather(fields, NOW)[0]["rain_mm_3h"])

    def test_no_cycle_gives_null_everywhere(self):
        out = weather.cell_weather(None, NOW, [1, 2], 3)
        self.assertEqual(len(out["now"]), 3)
        self.assertEqual(set(out["now"][0].values()), {None})
        self.assertEqual(set(out["days"][2][0].values()), {None})
        self.assertEqual(set(out["today"][2].values()), {None})


class Bundles(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cache = self._dir.name

    def test_decode_keeps_2m_temperature_humidity_and_cumulative_rain(self):
        path = os.path.join(self.cache, "x.grib")
        with open(path, "wb") as fh:
            fh.write(wx_bundle(6, temp_k=300.0, rh=55.0, cumulative=3.0))
        got = weather.decode_wx(path)
        self.assertEqual(set(got), {"t", "rh", "apcp"})
        self.assertAlmostEqual(float(got["t"][0][0][0]), 300.0, places=0)

    def test_load_cycle_samples_cells_and_converts_to_celsius(self):
        calls = []

        def fetch(cycle, fhr, kind, budget, log=print):
            calls.append(fhr)
            return wx_bundle(fhr, temp_k=303.15, rh=70.0)
        fields = weather.load_cycle(CYCLE, [0, 3, 6], LATS, LONS, self.cache, Budget(), fetch)
        self.assertAlmostEqual(float(fields.temp[3][0]), 30.0, delta=0.1)
        self.assertAlmostEqual(float(fields.acc[6][1]), 3.0, delta=0.01)
        weather.load_cycle(CYCLE, [0, 3, 6], LATS, LONS, self.cache, Budget(), fetch)
        self.assertEqual(calls, [0, 3, 6])  # the second load came from the cache

    def test_a_failed_hour_is_left_out_and_the_rest_kept(self):
        def fetch(cycle, fhr, kind, budget, log=print):
            if fhr == 3:
                raise PipelineError("not on the server")
            return wx_bundle(fhr)
        fields = weather.load_cycle(CYCLE, [0, 3, 6], LATS, LONS, self.cache, Budget(), fetch,
                                    log=lambda line: None)
        self.assertEqual(sorted(fields.temp), [0, 6])

    def test_a_cycle_that_gives_nothing_is_none_and_the_previous_cycle_is_tried(self):
        asked = []

        def fetch(cycle, fhr, kind, budget, log=print):
            asked.append(cycle)
            if cycle == CYCLE:
                raise PipelineError("cycle not published")
            return wx_bundle(fhr)
        fields = weather.load_weather(NOW, CYCLE, [1], LATS, LONS, self.cache, Budget(), fetch,
                                      log=lambda line: None)
        self.assertEqual(fields.cycle, CYCLE - timedelta(hours=6))
        self.assertIn(CYCLE, asked)

    def test_a_corrupt_cached_file_is_discarded(self):
        path = weather.gfs_extra.bundle_path(self.cache, CYCLE, 3, "wx")
        with open(path, "wb") as fh:
            fh.write(b"not a grib")
        fields = weather.load_cycle(CYCLE, [3], LATS, LONS, self.cache, Budget(),
                                    lambda *a, **k: wx_bundle(3), log=lambda line: None)
        self.assertIsNone(fields)
        self.assertFalse(os.path.exists(path))

    def test_needed_hours_cover_every_step_and_rain_edge(self):
        hours = weather.needed_hours(CYCLE, NOW, [1, 2, 3])
        self.assertIn(9, hours)  # nearest step to now
        self.assertIn(6, hours)  # the 3 h before it
        self.assertIn(15, hours)  # last step of today
        self.assertEqual(max(hours), 90)  # day +3 ends 22 Oct 16:00 UTC = f088, so f087 + 3
        self.assertEqual(hours, sorted(set(hours)))


class Fetch(unittest.TestCase):
    def test_query_asks_for_the_region_and_three_fields(self):
        url = weather.nomads_query(CYCLE, 6)
        for part in ("filter_gfs_0p50.pl", "gfs.t00z.pgrb2full.0p50.f006", "var_TMP=on",
                     "var_RH=on", "var_APCP=on", "lev_2_m_above_ground=on", "leftlon=94",
                     "rightlon=122", "toplat=10", "bottomlat=-4", "%2Fgfs.20261019%2F00%2Fatmos"):
            self.assertIn(part, url)

    def test_reply_that_is_not_grib_is_an_error(self):
        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<html>busy</html>"
        seen = {}

        def urlopen(req, timeout=None):
            seen["agent"] = req.get_header("User-agent")
            seen["timeout"] = timeout
            return Reply()
        with self.assertRaises(PipelineError):
            weather.fetch_wx(CYCLE, 6, Budget(), urlopen=urlopen, sleep=lambda s: None)
        self.assertEqual(seen, {"agent": "HazeMY-data", "timeout": 30})


if __name__ == "__main__":
    unittest.main()
