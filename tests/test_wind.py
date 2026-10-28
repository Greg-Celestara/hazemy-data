"""wind.json (windfile), five days of FIRMS history, hourly back-paths, the fixture script."""
import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy import firms, trajectory, windfile
from hazemy.common import Budget, PipelineError, fmt, parse_time
from hazemy.firms import fetch_detection_rows, history_urls, load_saved_rows, save_rows
from hazemy.job import run_job
from tests.test_data_sources import HEADER, row
from tests.test_job import ENV, NOW, FakeResponse, fake_retrieve, fake_urlopen, fake_wind
from tests.test_v2 import fake_bundle_factory

START = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
NEWEST = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
FIXTURE_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "wind_path_fixture.py")


def synthetic_doc(start=START, u=0.0, v=0.0) -> dict:
    """A valid wind.json with a uniform wind everywhere, built without GFS."""
    field = windfile.encode_field(np.full((windfile.NLAT, windfile.NLON), float(u)))
    field_v = windfile.encode_field(np.full((windfile.NLAT, windfile.NLON), float(v)))
    times = [{"valid_utc": fmt(t), "cycle_utc": fmt(t), "fhr": 0, "u": field, "v": field_v}
             for t in windfile.slice_times(start)]
    days = [{"start_utc": fmt(start - timedelta(hours=24 * (k + 1))),
             "end_utc": fmt(start - timedelta(hours=24 * k)), "bins": []} for k in range(5)]
    return {"schema": "hazemy-wind/1", "generated_utc": fmt(start), "start_utc": fmt(start),
            "gfs_cycle_utc": fmt(start), "level_hpa": 850, "grid": dict(windfile.GRID),
            "scale_ms": 0.5, "step_h": 3, "times": times, "fires_updated_utc": None,
            "provinces": [], "fires_days": days}


class Encoding(unittest.TestCase):
    def test_round_trip_is_within_a_quarter_of_a_metre_per_second(self):
        rng = np.random.default_rng(1)
        values = rng.uniform(-40, 40, (windfile.NLAT, windfile.NLON))
        back = windfile.decode_field(windfile.encode_field(values))
        self.assertLessEqual(np.abs(back - values).max(), 0.25 + 1e-9)

    def test_values_beyond_range_clip_to_plus_minus_63_5(self):
        values = np.zeros((windfile.NLAT, windfile.NLON))
        values[0, 0], values[0, 1] = 100.0, -100.0
        back = windfile.decode_field(windfile.encode_field(values))
        self.assertEqual((back[0, 0], back[0, 1]), (63.5, -63.5))
        raw = base64.b64decode(windfile.encode_field(values))
        self.assertNotIn(-128, np.frombuffer(raw, dtype=np.int8))

    def test_non_finite_values_are_refused(self):
        values = np.zeros((windfile.NLAT, windfile.NLON))
        values[3, 3] = np.nan
        with self.assertRaises(PipelineError):
            windfile.encode_field(values)

    def test_north_first_gfs_field_is_flipped_so_row_0_is_lat_minus_12(self):
        geometry = {"ni": 87, "nj": 69, "lat_first": 18.0, "lon_first": 86.0, "di": 0.5,
                    "dj": 0.5, "south_first": False}
        lats = 18.0 - 0.5 * np.arange(69)
        lons = 86.0 + 0.5 * np.arange(87)
        field = lats[:, None] + 0.0 * lons[None, :]
        grid = windfile.regrid(field, geometry)
        self.assertEqual(grid.shape, (53, 71))
        self.assertEqual((grid[0, 0], grid[-1, 0]), (-12.0, 14.0))
        field = lons[None, :] + 0.0 * lats[:, None]
        grid = windfile.regrid(field, geometry)
        self.assertEqual((grid[0, 0], grid[0, -1]), (90.0, 125.0))

    def test_grid_points_missing_from_the_field_raise(self):
        geometry = {"ni": 40, "nj": 69, "lat_first": 18.0, "lon_first": 86.0, "di": 0.5,
                    "dj": 0.5, "south_first": False}
        with self.assertRaises(PipelineError):
            windfile.regrid(np.zeros((69, 40)), geometry)


class TimeList(unittest.TestCase):
    def test_covers_96_h_back_and_78_h_ahead_for_any_hour(self):
        for hour in (0, 2, 5, 23):
            start = datetime(2026, 10, 19, hour, tzinfo=timezone.utc)
            times = windfile.slice_times(start)
            self.assertLessEqual(times[0], start - timedelta(hours=96), hour)
            self.assertGreaterEqual(times[-1], start + timedelta(hours=78), hour)
            self.assertIn(len(times), (59, 60), hour)
            self.assertTrue(all(b - a == timedelta(hours=3) for a, b in zip(times, times[1:])))
            self.assertTrue(all(t.hour % 3 == 0 for t in times))


def counting_fetch(inner, broken):
    """Fetch that raises for the cycles in `broken` (a predicate) and records the rest."""
    seen = []

    def fetch(cycle, fhr, kind, budget, log=print):
        if broken(cycle):
            raise PipelineError(f"GFS {cycle:%d %H} not on the bucket")
        seen.append((cycle, fhr))
        return inner(cycle, fhr, kind, budget, log)
    return fetch, seen


class CycleFallback(unittest.TestCase):
    def load(self, broken):
        fetch, seen = counting_fetch(fake_bundle_factory([]), broken)
        with tempfile.TemporaryDirectory() as tmp:
            return windfile.load_times(START, NEWEST, tmp, Budget(), fetch, lambda _: None), seen

    def test_newest_cycle_is_used_when_available(self):
        (entries, cycle), _ = self.load(lambda c: False)
        self.assertEqual(cycle, NEWEST)
        self.assertEqual(len(entries), len(windfile.slice_times(START)))
        future = [e for e in entries if parse_time(e["valid_utc"]) > NEWEST]
        self.assertTrue(all(e["cycle_utc"] == fmt(NEWEST) for e in future))
        self.assertEqual(future[-1]["fhr"], 90)

    def test_previous_cycle_is_used_when_the_newest_fails(self):
        (entries, cycle), seen = self.load(lambda c: c == NEWEST)
        self.assertEqual(cycle, NEWEST - timedelta(hours=6))
        self.assertNotIn(NEWEST, {c for c, _ in seen})
        self.assertEqual(entries[-1]["cycle_utc"], fmt(cycle))

    def test_both_cycles_failing_raises(self):
        broken = lambda c: c >= NEWEST - timedelta(hours=6)  # noqa: E731
        with self.assertRaises(PipelineError):
            self.load(broken)


def fire(lat, lon, hours_ago):
    return (lat, lon, START - timedelta(hours=hours_ago))


class FireDays(unittest.TestCase):
    def days(self, rows, located):
        return windfile.fire_days(rows, located, START)

    def test_ages_land_in_the_right_day_or_nowhere(self):
        rows = [fire(0.5, 101.5, 1), fire(1.5, 102.5, 25), fire(2.5, 103.5, 119),
                fire(3.5, 104.5, 121)]
        _, days = self.days(rows, [("Riau", "ID")] * 4)
        counts = [sum(b[2] for b in d["bins"]) for d in days]
        self.assertEqual(counts, [1, 1, 0, 0, 1])
        self.assertEqual(days[0]["end_utc"], fmt(START))
        self.assertEqual(days[0]["start_utc"], fmt(START - timedelta(hours=24)))
        self.assertEqual(days[4]["start_utc"], fmt(START - timedelta(hours=120)))

    def test_province_index_and_minus_one_for_sea(self):
        rows = [fire(0.5, 101.5, 1), fire(5.0, 98.0, 2)]
        names, days = self.days(rows, [("Riau", "ID"), None])
        self.assertEqual(names, [["Riau", "ID"]])
        by_lat = {b[0]: b[3] for b in days[0]["bins"]}
        self.assertEqual(by_lat, {0.625: 0, 5.125: -1})

    def test_most_common_province_wins_and_ties_go_to_the_first_name(self):
        same_bin = [fire(0.5, 101.5, 1)] * 3
        names, days = self.days(same_bin, [("Riau", "ID"), ("Jambi", "ID"), ("Jambi", "ID")])
        self.assertEqual(names[days[0]["bins"][0][3]], ["Jambi", "ID"])
        names, days = self.days(same_bin[:2], [("Riau", "ID"), ("Jambi", "ID")])
        self.assertEqual(names[days[0]["bins"][0][3]], ["Jambi", "ID"])

    def test_provinces_list_only_used_ones_sorted(self):
        rows = [fire(0.5, 101.5, 1), fire(2.0, 112.5, 30)]
        names, _ = self.days(rows, [("Sarawak", "MY"), ("Riau", "ID")])
        self.assertEqual(names, [["Riau", "ID"], ["Sarawak", "MY"]])

    def test_a_day_is_capped_at_1500_bins(self):
        rows = [fire(lat, lon, 1) for lat in np.arange(-6.0, 7.0, 0.25)
                for lon in np.arange(95.0, 102.75, 0.25)]
        self.assertGreater(len(rows), 1500)
        names, days = self.days(rows, [None] * len(rows))
        self.assertEqual((len(days[0]["bins"]), names), (1500, []))

    def test_no_detections_gives_five_empty_days(self):
        names, days = self.days([], [])
        self.assertEqual((names, [d["bins"] for d in days]), ([], [[]] * 5))


class Validate(unittest.TestCase):
    def test_a_valid_document_gives_compact_json(self):
        body = windfile.validate(synthetic_doc())
        self.assertNotIn(b" ", body)
        self.assertLess(len(body), windfile.MAX_BYTES)

    def invalid(self, change):
        doc = synthetic_doc()
        change(doc)
        with self.assertRaises(PipelineError):
            windfile.validate(doc)

    def test_gap_in_times_is_rejected(self):
        self.invalid(lambda d: d["times"].pop(10))

    def test_times_not_reaching_96_h_back_are_rejected(self):
        self.invalid(lambda d: d["times"].pop(0))

    def test_times_not_reaching_78_h_ahead_are_rejected(self):
        self.invalid(lambda d: d["times"].pop())

    def test_wrong_byte_length_is_rejected(self):
        short = base64.b64encode(bytes(windfile.FIELD_BYTES - 1)).decode()
        self.invalid(lambda d: d["times"][3].update(u=short))

    def test_bad_base64_is_rejected(self):
        self.invalid(lambda d: d["times"][3].update(v="not base64!"))

    def test_province_index_out_of_range_is_rejected(self):
        self.invalid(lambda d: d["fires_days"][0]["bins"].append([1.0, 100.0, 2, 0]))

    def test_wrong_number_of_fire_days_is_rejected(self):
        self.invalid(lambda d: d["fires_days"].pop())

    def test_wrong_grid_or_schema_is_rejected(self):
        self.invalid(lambda d: d["grid"].update(nlat=52))
        self.invalid(lambda d: d.update(schema="hazemy-wind/2"))

    def test_oversize_body_is_rejected(self):
        self.invalid(lambda d: d["fires_days"][0]["bins"].extend([[1.0, 100.0, 2, -1]] * 40000))


class HourlyPaths(unittest.TestCase):
    def test_every_third_hourly_point_equals_the_default_path(self):
        geom = dict(windfile.DECODED_GEOMETRY)
        times = [START - timedelta(hours=48) + timedelta(hours=3 * k) for k in range(17)]
        u = 3.0 + 0.1 * np.tile(np.arange(71), (53, 1))
        v = 1.0 + 0.05 * np.tile(np.arange(53)[:, None], (1, 71))
        fields = {t: (u * (1 + 0.1 * k), v, geom) for k, t in enumerate(times)}
        args = (fields, times, [2.0, 5.0], [110.0, 115.0], times[-1])
        default = trajectory.back_trajectories(*args)
        hourly = trajectory.back_trajectories(*args, every_h=1)
        for d, h in zip(default, hourly):
            self.assertEqual((len(d), len(h)), (17, 49))
            self.assertEqual(h[::3], d)

    def test_every_h_must_divide_48(self):
        with self.assertRaises(PipelineError):
            trajectory.back_trajectories({}, [START, START], [0.0], [100.0], every_h=5)

    def test_decoded_uniform_wind_moves_the_path_the_expected_distance(self):
        doc = synthetic_doc(u=10.0)
        fields, times = windfile.decode(doc)
        (path,) = trajectory.back_trajectories(fields, times, [0.0], [120.0], START,
                                               every_h=1)
        self.assertEqual(len(path), 49)
        self.assertAlmostEqual(trajectory.distance_km(path[0], path[-1]), 1728.0, delta=1.0)
        self.assertLess(path[-1][1], path[0][1])  # an east wind: the air came from the west

    def test_decode_returns_the_shape_back_trajectories_takes(self):
        fields, times = windfile.decode(synthetic_doc(u=2.0, v=-1.0))
        u, v, geometry = fields[times[0]]
        self.assertEqual((u.shape, geometry["south_first"], geometry["lat_first"]),
                         ((53, 71), True, -12.0))
        self.assertEqual((u[0, 0], v[0, 0]), (2.0, -1.0))


class FixtureScript(unittest.TestCase):
    def test_five_cases_with_hourly_points(self):
        spec = importlib.util.spec_from_file_location("wind_path_fixture", FIXTURE_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from hazemy.job import load_towns
        result = module.build(synthetic_doc(u=2.0), load_towns())
        self.assertEqual(result["source"], fmt(START))
        names = [(c["town"], c["arrive_utc"]) for c in result["cases"]]
        self.assertEqual(names, [
            ("Kuala Lumpur", fmt(START)), ("Kuching", fmt(START)),
            ("Kota Kinabalu", fmt(START)),
            ("Kuala Lumpur", fmt(START - timedelta(hours=30))),
            ("Johor Bahru", fmt(START + timedelta(hours=33)))])
        for case in result["cases"]:
            self.assertEqual(len(case["points"]), 49)
            self.assertEqual(case["points"][0], [round(case["lat"], 4), round(case["lon"], 4)])


class FirmsHistory(unittest.TestCase):
    def test_two_urls_per_source_with_the_right_count_and_date(self):
        urls = history_urls("KEY", "VIIRS_SNPP_NRT", NOW)
        base = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/KEY/VIIRS_SNPP_NRT/95,-6,120,8"
        self.assertEqual(urls, [f"{base}/5", f"{base}/1/2026-10-14"])

    def test_fetch_requests_both_urls_for_every_source_and_joins_the_rows(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            return FakeResponse(HEADER + row(0.5, 101.5, date="2026-10-14", time="1500"))
        rows = fetch_detection_rows("K", NOW, Budget(), log=lambda _: None, urlopen=urlopen,
                                    sleep=lambda s: None)
        self.assertEqual(len(seen), 2 * len(firms.SOURCES))
        self.assertEqual([u.rsplit("/", 1)[-1] for u in seen[:2]], ["5", "2026-10-14"])
        self.assertEqual(len(rows), len(seen))  # 99.5 h old: inside 120 h, outside 72 h

    def test_parse_rows_default_window_is_still_72_h(self):
        text = HEADER + row(1, 100, date="2026-10-15", time="1000") + row(2, 101)
        self.assertEqual(len(firms.parse_rows(text, "T", NOW)), 1)
        self.assertEqual(len(firms.parse_rows(text, "T", NOW, firms.HISTORY_H)), 2)

    def test_saved_copy_keeps_120_h_rows_and_drops_older_ones(self):
        rows = [(1.0, 100.0, NOW - timedelta(hours=100)), (2.0, 101.0, NOW - timedelta(hours=119)),
                (3.0, 102.0, NOW - timedelta(hours=121))]
        with tempfile.TemporaryDirectory() as tmp:
            save_rows(tmp, rows, NOW)
            loaded, _ = load_saved_rows(tmp, NOW)
        self.assertEqual([r[0] for r in loaded], [1.0, 2.0])
        with tempfile.TemporaryDirectory() as tmp:
            save_rows(tmp, rows, NOW)
            loaded, _ = load_saved_rows(tmp, NOW + timedelta(hours=2))
        self.assertEqual([r[0] for r in loaded], [1.0])


class WindJob(unittest.TestCase):
    FIRES = HEADER + row(0.5, 101.5) * 3 + row(2.0, 112.5)

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        self.lines = []

    def run_it(self, csv_text=None, broken=None, with_wind=True, root=None):
        root = root or self.root
        csv_text = csv_text or self.FIRES
        fetch, _ = counting_fetch(fake_bundle_factory([]), broken or (lambda c: False))
        paths = {k: os.path.join(root, k, "x.json") for k in ("v1", "v2", "wind")}

        def urlopen(req, timeout=None):
            if "data.gov.sg" in req.full_url:
                raise OSError("NEA down")  # v2 publishes without it
            return fake_urlopen(csv_text)(req)
        code = run_job(ENV, out=paths["v1"], out_v2=paths["v2"],
                       out_wind=paths["wind"] if with_wind else None,
                       cache_dir=os.path.join(root, "cache"),
                       gfs_cache_dir=os.path.join(root, "gfs"), now_fn=lambda: NOW,
                       log=self.lines.append, retrieve=fake_retrieve, fetch_wind=fake_wind,
                       fetch_bundle=fetch,
                       http={"urlopen": urlopen, "sleep": lambda s: None})
        self.paths = paths
        return code

    def read(self, key):
        with open(self.paths[key], "rb") as fh:
            return fh.read()

    def test_job_writes_a_wind_json_that_validates(self):
        self.assertEqual(self.run_it(), 0)
        body = self.read("wind")
        doc = json.loads(body)
        self.assertEqual(windfile.validate(doc), body)
        self.assertEqual(doc["start_utc"], "2026-10-19T10:00:00Z")
        self.assertEqual(doc["gfs_cycle_utc"], "2026-10-19T00:00:00Z")
        self.assertEqual(doc["provinces"], [["Riau", "ID"], ["Sarawak", "MY"]])
        self.assertEqual(sorted((b[0], b[1], b[2], b[3]) for b in doc["fires_days"][0]["bins"]),
                         [(0.625, 101.625, 9, 0), (2.125, 112.625, 3, 1)])  # x3 sources
        self.assertTrue(any("bytes gzip)" in line for line in self.lines))
        u, _, _ = windfile.decode(doc)[0][parse_time(doc["times"][0]["valid_utc"])]
        self.assertEqual(float(u[0, 0]), 4.0)  # the fake bundle blows 4 m/s eastwards

    def test_without_out_wind_no_file_is_built(self):
        self.assertEqual(self.run_it(with_wind=False), 0)
        self.assertFalse(os.path.exists(self.paths["wind"]))

    def test_unavailable_gfs_history_leaves_v1_and_v2_and_no_wind_json(self):
        too_old = lambda c: c < NEWEST - timedelta(hours=48)  # noqa: E731
        self.assertEqual(self.run_it(broken=too_old), 0)
        self.assertTrue(os.path.exists(self.paths["v1"]))
        self.assertTrue(os.path.exists(self.paths["v2"]))
        self.assertFalse(os.path.exists(self.paths["wind"]))
        errors = [line for line in self.lines if line.startswith("ERROR: wind.json failed")]
        self.assertEqual(len(errors), 1)
        self.assertIn("publishing without it", errors[0])

    def test_older_detections_do_not_change_v1_or_v2(self):
        self.assertEqual(self.run_it(), 0)
        v1, v2 = self.read("v1"), self.read("v2")
        older = self.FIRES + row(0.5, 101.5, date="2026-10-15", time="1000") * 4
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(self.run_it(csv_text=older, root=other, with_wind=False), 0)
            self.assertEqual((self.read("v1"), self.read("v2")), (v1, v2))


class SizeLineTest(unittest.TestCase):
    def test_warns_only_over_target(self):
        small = windfile.size_line("w.json", b"{}")
        big = windfile.size_line("w.json", os.urandom(windfile.GZIP_TARGET_BYTES + 1000))
        self.assertNotIn("WARNING", small)
        self.assertIn("WARNING", big)


class RealFixtureTest(unittest.TestCase):
    """The app's Kotlin path test compares against tests/fixtures/wind_paths.json, made from the
    real wind.json of 26 Oct 2026 beside it. This pins the Python side of that comparison."""

    def test_fixture_paths_reproduce(self):
        here = os.path.join(os.path.dirname(__file__), "fixtures")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "paths.json")
            subprocess.run([sys.executable, os.path.join("scripts", "wind_path_fixture.py"),
                            os.path.join(here, "wind.json"), out],
                           check=True, capture_output=True)
            with open(out) as fh, open(os.path.join(here, "wind_paths.json")) as ref:
                self.assertEqual(json.load(fh), json.load(ref))


if __name__ == "__main__":
    unittest.main()
