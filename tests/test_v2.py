"""v2 additions: Singapore/NEA, trajectories, drivers, fires, bundles and the two-file job."""
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy import drivers, firms, gfs_extra, output, output_v2, singapore, trajectory
from hazemy import grid as gridmod
from hazemy.common import Budget, PipelineError
from hazemy.job import run_job
from tests.gribmaker import make_message
from tests.test_data_sources import HEADER, row
from tests.test_job import ENV, NOW, FakeResponse, fake_retrieve, fake_urlopen, fake_wind

CYCLE = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
GEOM = {"ni": 91, "nj": 81, "lat_first": 20.0, "lon_first": 85.0, "di": 0.5, "dj": 0.5,
        "south_first": False}


def const_fields(times, u, v):
    return {t: (np.full((81, 91), u, float), np.full((81, 91), v, float), GEOM) for t in times}


def grid_times(n=17):
    return [CYCLE + timedelta(hours=3 * k) for k in range(n)]


class Psi(unittest.TestCase):
    def test_nea_worked_example(self):
        self.assertEqual(singapore.pm25_to_psi(40), 83)  # NEA computation document, example

    def test_breakpoints(self):
        cases = {0: 0, 12: 50, 55: 100, 150: 200, 250: 300, 350: 400, 500: 500, 900: 500}
        for conc, psi in cases.items():
            self.assertEqual(singapore.pm25_to_psi(conc), psi, conc)

    def test_negative_is_zero(self):
        self.assertEqual(singapore.pm25_to_psi(-1.0), 0)

    def test_segment_edges_follow_the_nea_table(self):
        cases = {12: 50, 13: 51, 55: 100, 56: 101, 150: 200, 151: 201, 250: 300, 251: 301,
                 350: 400, 351: 401, 500: 500, 501: 500}
        for conc, psi in cases.items():
            self.assertEqual(singapore.pm25_to_psi(conc), psi, conc)

    def test_concentration_is_rounded_to_an_integer_first(self):
        self.assertEqual(singapore.pm25_to_psi(12.4), 50)  # rounds to 12
        self.assertEqual(singapore.pm25_to_psi(12.5), 51)  # rounds to 13: a new segment


class SingaporeCells(unittest.TestCase):
    def test_sg_only_within_30_km_of_singapore(self):
        land = singapore.load_land_km()
        cells = [(0, 0, 1.2, 103.6), (0, 0, 1.6, 104.0), (0, 0, 3.2, 101.6),
                 (0, 0, 2.4, 103.6)]
        self.assertEqual(singapore.sg_flags(cells, land), [True, True, False, False])

    def test_coverage_land_includes_singapore(self):
        land = singapore.load_land_km()
        self.assertTrue(gridmod.land_cells(land))

    def test_towns_have_a_valid_region(self):
        towns = singapore.load_towns()
        self.assertGreaterEqual(len(towns), 20)
        self.assertTrue(all(t["country"] == "SG" and t["nea_region"] in singapore.NEA_REGIONS
                            for t in towns))


def nea_body(psi=None, regions=singapore.NEA_REGIONS):
    psi = psi or {r: 60 + k for k, r in enumerate(singapore.NEA_REGIONS)}
    meta = [{"name": r, "labelLocation": {"latitude": 1.3, "longitude": 103.8}} for r in regions]
    item = {"timestamp": "2026-10-20T11:00:00+08:00",
            "readings": {"psi_twenty_four_hourly": {r: psi[r] for r in regions}}}
    return json.dumps({"code": 0, "data": {"regionMetadata": meta, "items": [item]}})


class Nea(unittest.TestCase):
    def test_parse(self):
        nea = singapore.parse_nea(nea_body({"north": 101, "south": 50, "east": 51, "west": 201,
                                            "central": 301}))
        self.assertEqual(nea["updated_utc"], "2026-10-20T03:00:00Z")
        bands = {k: v["band"] for k, v in nea["regions"].items()}
        self.assertEqual(bands, {"north": 3, "south": 1, "east": 2, "west": 4, "central": 5})

    def test_latest_item_is_used(self):
        data = json.loads(nea_body())
        old = json.loads(json.dumps(data["data"]["items"][0]))
        old["timestamp"] = "2026-10-20T10:00:00+08:00"
        old["readings"]["psi_twenty_four_hourly"]["north"] = 999
        data["data"]["items"].insert(0, old)
        self.assertEqual(singapore.parse_nea(json.dumps(data))["regions"]["north"]["psi_24h"], 60)

    def test_missing_region_raises(self):
        with self.assertRaises(PipelineError):
            singapore.parse_nea(nea_body(regions=("north", "south", "east", "west")))

    def test_garbage_raises(self):
        with self.assertRaises(PipelineError):
            singapore.parse_nea("<html>maintenance</html>")

    def test_fetch_failure_gives_none(self):
        logs = []

        def boom(req, timeout=None):
            raise OSError("down")
        got = singapore.fetch_nea(NOW, Budget(), log=logs.append, urlopen=boom,
                                  sleep=lambda s: None)
        self.assertIsNone(got)
        self.assertTrue(any("omitting 'nea'" in line for line in logs))

    def test_attribution_has_access_date(self):
        self.assertIn("accessed on 19 Oct 2026 from data.gov.sg", singapore.attribution(NOW))


class Trajectory(unittest.TestCase):
    def run_paths(self, u, v, lats, lons):
        times = grid_times()
        return trajectory.back_trajectories(const_fields(times, u, v), times, lats, lons)

    def test_westerly_wind_path_heads_west_at_wind_speed(self):
        (path,) = self.run_paths(5.0, 0.0, [0.0], [110.0])
        self.assertEqual(len(path), 17)
        km = 5.0 * 3.6 * 3
        for a, b in zip(path, path[1:]):
            self.assertAlmostEqual(trajectory.distance_km(a, b), km, delta=0.5)
            self.assertAlmostEqual(trajectory.bearing_deg(a, b), 270, delta=1)

    def test_south_wind_path_heads_south(self):
        (path,) = self.run_paths(0.0, 4.0, [5.0], [105.0])
        self.assertAlmostEqual(trajectory.bearing_deg(path[0], path[1]), 180, delta=1)

    def test_path_is_continuous_in_shear(self):
        times = grid_times()
        u = np.tile(np.linspace(8, -8, 91), (81, 1))
        fields = {t: (u, np.zeros((81, 91)), GEOM) for t in times}
        (path,) = trajectory.back_trajectories(fields, times, [3.0], [107.0])
        steps = [trajectory.distance_km(a, b) for a, b in zip(path, path[1:])]
        self.assertTrue(all(s < 160 for s in steps), steps)

    def test_path_stops_at_the_bbox_edge(self):
        (path,) = self.run_paths(5.0, 0.0, [0.0], [91.0])
        self.assertEqual(len(path), 3)
        self.assertTrue(all(lon >= 90.0 for _, lon in path))

    def test_stopped_path_does_not_affect_others(self):
        short, long = self.run_paths(5.0, 0.0, [0.0, 0.0], [91.0, 110.0])
        self.assertEqual((len(short), len(long)), (3, 17))

    def test_cycles_and_times_cover_48_hours(self):
        cycles = trajectory.trajectory_cycles(CYCLE)
        start = CYCLE + timedelta(hours=10)
        times = trajectory.slice_times(cycles, start)
        self.assertEqual(len(cycles), 9)
        self.assertLessEqual(times[0], start - timedelta(hours=48))
        self.assertGreaterEqual(times[-1], start)

    def test_slices_continue_past_the_newest_cycle_with_forecast_steps(self):
        cycles = trajectory.trajectory_cycles(CYCLE)
        for hours in (5, 9, 10):
            times = trajectory.slice_times(cycles, CYCLE + timedelta(hours=hours))
            steps = {(b - a) for a, b in zip(times, times[1:])}
            self.assertEqual(steps, {timedelta(hours=3)})
            self.assertGreaterEqual(times[-1], CYCLE + timedelta(hours=hours))
            self.assertLess(times[-1], CYCLE + timedelta(hours=hours + 3))

    def test_start_is_the_generation_hour_rounded_down(self):
        self.assertEqual(trajectory.start_time(NOW),
                         datetime(2026, 10, 19, 10, tzinfo=timezone.utc))

    def test_point_times_label_24h_and_48h_points(self):
        start = trajectory.start_time(NOW)
        labels = trajectory.point_times(start)
        self.assertEqual((labels[0], labels[8], labels[16]),
                         (start, start - timedelta(hours=24), start - timedelta(hours=48)))

    def test_path_starts_now_and_integrates_the_wind_of_the_matching_hours(self):
        # u grows 0.1 m/s per hour since the first slice, so displacement depends on the clock.
        start = CYCLE + timedelta(hours=10)
        times = trajectory.slice_times(trajectory.trajectory_cycles(CYCLE), start)
        fields = {t: (np.full((81, 91), 0.1 * (t - times[0]).total_seconds() / 3600),
                      np.zeros((81, 91)), GEOM) for t in times}
        (path,) = trajectory.back_trajectories(fields, times, [0.0], [110.0], start)
        self.assertEqual(path[0], (0.0, 110.0))

        def km_west(point_index):
            return (110.0 - path[point_index][1]) * trajectory.KM_PER_DEG_LAT

        def expected_km(hours_back):
            h0 = (start - times[0]).total_seconds() / 3600
            lo, hi = h0 - hours_back, h0
            return 0.1 * (hi ** 2 - lo ** 2) / 2 * 3.6  # integral of u(h) dh, km

        self.assertAlmostEqual(km_west(8), expected_km(24), delta=0.5)
        self.assertAlmostEqual(km_west(16), expected_km(48), delta=0.5)

    def test_start_beyond_the_slices_raises(self):
        times = grid_times()
        with self.assertRaises(PipelineError):
            trajectory.back_trajectories(const_fields(times, 1, 1), times, [0.0], [100.0],
                                         times[-1] + timedelta(hours=3))

    def test_too_few_slices_raise(self):
        times = grid_times(10)
        with self.assertRaises(PipelineError):
            trajectory.back_trajectories(const_fields(times, 1, 1), times, [0.0], [100.0])


def det(rows, now=NOW, provinces=None):
    located = provinces or [("Riau", "ID")] * len(rows)
    return drivers.Detections(rows, located, now)


def fire(lat, lon, hours_ago):
    return (lat, lon, NOW - timedelta(hours=hours_ago))


class PathFires(unittest.TestCase):
    PATH = [[2.0, 102.0], [2.0, 103.0]]

    def test_buffer_is_25_km(self):
        inside = fire(2.0 + 24 / 111.195, 102.0, 1)
        outside = fire(2.0 + 26 / 111.195, 102.0, 1)
        stats = drivers.path_fire_stats(self.PATH, det([inside, outside]))
        self.assertEqual(stats["path_fire_count_72h"], 1)

    def test_buffer_applies_to_any_path_point(self):
        stats = drivers.path_fire_stats(self.PATH, det([fire(2.0, 103.0, 1)]))
        self.assertEqual(stats["path_fire_count_72h"], 1)

    def test_no_fires_gives_nulls(self):
        stats = drivers.path_fire_stats(self.PATH, det([fire(8.0, 120.0, 1)]))
        self.assertEqual(stats, {"path_fire_province": None, "path_fire_count_72h": 0,
                                 "path_fire_trend": None})

    def test_detections_outside_provinces_keep_the_count_with_a_null_province(self):
        stats = drivers.path_fire_stats(self.PATH, det([fire(2.0, 102.0, 1)] * 2,
                                                       provinces=[None, None]))
        self.assertEqual((stats["path_fire_province"], stats["path_fire_count_72h"]), (None, 2))
        self.assertEqual(stats["path_fire_trend"], "rising")

    def test_province_with_most_detections_wins(self):
        rows = [fire(2.0, 102.0, 1)] * 3
        located = [("Riau", "ID"), ("Jambi", "ID"), ("Jambi", "ID")]
        self.assertEqual(drivers.path_fire_stats(self.PATH, det(rows, provinces=located))
                         ["path_fire_province"], "Jambi")

    def test_trend_thresholds(self):
        cases = [(13, 10, "rising"), (12, 10, "steady"), (8, 10, "steady"), (7, 10, "falling"),
                 (1, 0, "rising"), (0, 0, "steady")]
        for recent, prior, want in cases:
            self.assertEqual(drivers._trend(recent, prior), want, (recent, prior))

    def test_trend_uses_last_24h_against_the_24h_before(self):
        rows = ([fire(2.0, 102.0, 5)] * 13 + [fire(2.0, 102.0, 30)] * 10
                + [fire(2.0, 102.0, 60)] * 99)
        self.assertEqual(drivers.path_fire_stats(self.PATH, det(rows))["path_fire_trend"],
                         "rising")


class FireProducts(unittest.TestCase):
    def test_daily_counts_oldest_first_for_the_72h_provinces(self):
        rows = [fire(0, 100, 60), fire(0, 100, 30), fire(0, 100, 30), fire(0, 100, 1),
                fire(1, 100, 1)]
        located = [("A", "ID"), ("A", "ID"), ("A", "ID"), ("A", "ID"), ("B", "ID")]
        got = drivers.fires_daily([{"province": "A", "country": "ID", "count": 4}],
                                  det(rows, provinces=located))
        self.assertEqual(got, [{"province": "A", "country": "ID", "counts": [1, 2, 1]}])

    def test_bin_centres(self):
        bins = drivers.fire_bins(det([fire(-2.2, 113.9, 1), fire(-2.1, 113.8, 2)]))
        self.assertEqual(bins, [[-2.125, 113.875, 2]])

    def test_bins_are_capped_largest_kept(self):
        rows = [fire(0.1 + 0.25 * k, 100.1, 1) for k in range(1600)]
        rows += [fire(-5.9, 100.1, 1)] * 5
        bins = drivers.fire_bins(det(rows))
        self.assertEqual(len(bins), 1500)
        self.assertEqual(bins[0][2], 5)

    def test_locate_provinces_marks_sea_as_none(self):
        provinces = gridmod.load_provinces()
        got = firms.locate_provinces([(3.1, 101.6), (0.0, 90.0)], provinces)
        self.assertEqual(got[0][1], "MY")
        self.assertIsNone(got[1])


class Bundles(unittest.TestCase):
    def test_idx_picks_850_wind_and_first_cumulative_apcp(self):
        text = "\n".join([
            "1:0:d=2026101906:UGRD:850 mb:3 hour fcst:",
            "2:100:d=2026101906:VGRD:850 mb:3 hour fcst:",
            "3:200:d=2026101906:APCP:surface:0-3 hour acc fcst:",
            "4:300:d=2026101906:APCP:surface:0-3 hour acc fcst:",
            "5:400:d=2026101906:TMP:2 m above ground:3 hour fcst:"])
        entries = gfs_extra.parse_idx_tail(text)
        self.assertEqual(gfs_extra.select_ranges(entries, "w850", 3),
                         [(0, 99, "UGRD"), (100, 199, "VGRD")])
        self.assertEqual(gfs_extra.select_ranges(entries, "rain", 3), [(200, 299, "APCP")])

    def test_12h_file_takes_the_zero_to_n_message_not_the_6_hour_bucket(self):
        text = ("1:0:d=x:APCP:surface:6-12 hour acc fcst:\n2:50:d=x:APCP:surface:0-12 hour acc "
                "fcst:\n3:90:d=x:TMP:2 m above ground:12 hour fcst:")
        got = gfs_extra.select_ranges(gfs_extra.parse_idx_tail(text), "rain", 12)
        self.assertEqual(got, [(50, 89, "APCP")])

    def test_missing_field_raises(self):
        with self.assertRaises(PipelineError):
            gfs_extra.select_ranges(gfs_extra.parse_idx_tail("1:0:d=x:TMP:2 m:anl:\n"), "w850", 0)

    IDX = ("1:0:d=x:UGRD:850 mb:3 hour fcst:\n2:10:d=x:VGRD:850 mb:3 hour fcst:\n"
           "3:20:d=x:TMP:2 m above ground:3 hour fcst:")

    def fetch_with(self, status, body_for):
        """fetch_bundle against a server that answers every range with `status`."""
        class Res:
            def __init__(self, status, body):
                self.status, self.body = status, body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def urlopen(req, timeout=None):
            if req.full_url.endswith(".idx"):
                return Res(200, self.IDX.encode())
            return Res(status, body_for(req.get_header("Range")))
        return gfs_extra.fetch_bundle(CYCLE, 3, "w850", Budget(), urlopen=urlopen,
                                      sleep=lambda s: None)

    def test_range_answers_206_and_200_are_accepted_when_the_length_matches(self):
        for status in (206, 200):
            got = self.fetch_with(status, lambda rng: b"x" * 10)
            self.assertEqual(got, b"x" * 20, status)

    def test_a_200_with_the_whole_file_is_a_failure(self):
        with self.assertRaises(PipelineError):
            self.fetch_with(200, lambda rng: b"x" * 25)

    def test_a_206_with_the_wrong_length_is_a_failure(self):
        with self.assertRaises(PipelineError):
            self.fetch_with(206, lambda rng: b"x" * 9)

    def test_each_bundle_is_fetched_once(self):
        calls = []

        def fetch(cycle, fhr, kind, budget, log=print):
            calls.append((cycle, fhr, kind))
            return b"GRIB"
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                gfs_extra.cached_bundle(tmp, CYCLE, 3, "w850", Budget(), fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(os.listdir(tmp), ["gfs_2026101906_f003_w850.grib"])

    def test_prune_removes_only_old_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept_edge = "gfs_2026101500_f000_w850.grib"  # exactly 102 h (KEEP_H) before CYCLE
            for name in ("gfs_2026101906_f000_w850.grib", kept_edge,
                         "gfs_2026101418_f000_w850.grib", "cams_x.grib"):
                open(os.path.join(tmp, name), "wb").close()
            gfs_extra.prune_cache(tmp, CYCLE)
            self.assertEqual(sorted(os.listdir(tmp)),
                             ["cams_x.grib", kept_edge, "gfs_2026101906_f000_w850.grib"])

    def test_decode_crops_and_checks_count(self):
        raw = make_message(np.arange(81 * 181, dtype=float).reshape(81, 181), lat_first=40.0,
                           lon_first=60.0, di=0.5, dj=0.5, param_id=131)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.grib")
            with open(path, "wb") as fh:
                fh.write(raw * 2)
            (field, geom), _ = gfs_extra.decode_bundle(path, 2)
            self.assertEqual(geom["lon_first"], 86.0)
            self.assertEqual(geom["lat_first"], 18.0)
            self.assertEqual(field.shape, (geom["nj"], geom["ni"]))
            with self.assertRaises(PipelineError):
                gfs_extra.decode_bundle(path, 3)


def fake_bundle_factory(calls):
    def msg(value, step=0):
        return make_message(np.full((81, 91), value), lat_first=20.0, lon_first=85.0, di=0.5,
                            dj=0.5, param_id=131, step=step)

    def fetch(cycle, fhr, kind, budget, log=print):
        calls.append((cycle, fhr, kind))
        if kind == "w850":
            return msg(4.0) + msg(-3.0)  # u east, v south: wind from the north-west
        return msg(0.5 * fhr)  # 0.5 mm per forecast hour
    return fetch


class TrajectoryWindFallback(unittest.TestCase):
    NEWEST = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)

    def run_loader(self, tmp, broken):
        calls = []
        inner = fake_bundle_factory(calls)

        def fetch(cycle, fhr, kind, budget, log=print):
            if cycle in broken:
                raise PipelineError(f"GFS {cycle:%d %H} not on the bucket")
            return inner(cycle, fhr, kind, budget, log)
        return drivers.load_trajectory_wind(NOW, self.NEWEST, set(), tmp, Budget(), fetch,
                                            log=lambda line: None)

    def test_newest_cycle_is_used_when_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            fields, times, cycles, used = self.run_loader(tmp, broken=())
        self.assertEqual(used, self.NEWEST)
        self.assertEqual(cycles[-1], self.NEWEST)
        self.assertTrue(set(times) <= set(fields))
        self.assertGreaterEqual(times[-1], trajectory.start_time(NOW))

    def test_previous_cycle_is_used_when_the_newest_is_not_on_the_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            fields, times, cycles, used = self.run_loader(tmp, broken=(self.NEWEST,))
        self.assertEqual(used, self.NEWEST - timedelta(hours=6))
        self.assertEqual(cycles[-1], used)
        self.assertGreaterEqual(times[-1], trajectory.start_time(NOW))
        self.assertTrue(set(times) <= set(fields))

    def test_both_cycles_missing_raises(self):
        broken = (self.NEWEST, self.NEWEST - timedelta(hours=6))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError):
            self.run_loader(tmp, broken=broken)


class JobV2(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        self.calls = []
        self.lines = []

    FIRES = HEADER + row(0.5, 101.5) * 3 + row(2.0, 112.5)

    def run_it(self, csv_text=FIRES, nea=None, now=NOW, firms_down=False, retrieve=fake_retrieve,
               bundle=None):
        def urlopen(req, timeout=None):
            if firms_down and "firms" in req.full_url:
                raise urllib.error.URLError("Network is unreachable")
            if "data.gov.sg" in req.full_url:
                if nea is None:
                    raise OSError("NEA down")
                return fake_urlopen(nea)(req)
            return fake_urlopen(csv_text)(req)
        self.out = os.path.join(self.root, "v1", "hazemy.json")
        self.out2 = os.path.join(self.root, "v2", "hazemy.json")
        code = run_job(ENV, out=self.out, out_v2=self.out2,
                       cache_dir=os.path.join(self.root, "cache"),
                       gfs_cache_dir=os.path.join(self.root, "gfs"), now_fn=lambda: now,
                       log=self.lines.append, retrieve=retrieve, fetch_wind=fake_wind,
                       fetch_bundle=bundle or fake_bundle_factory(self.calls),
                       http={"urlopen": urlopen, "sleep": lambda s: None})
        return code

    def test_firms_down_with_a_fresh_saved_copy_reuses_it(self):
        self.assertEqual(self.run_it(), 0)
        later = NOW + timedelta(hours=8)
        self.assertEqual(self.run_it(now=later, firms_down=True), 0)
        v1, v2 = self.read(self.out), self.read(self.out2)
        stamp = "2026-10-19T10:30:00Z"
        self.assertEqual((v1["sources"]["firms_updated_utc"], v2["sources"]["firms_updated_utc"]),
                         (stamp, stamp))
        self.assertEqual(v1["fires_72h"][0], {"province": "Riau", "country": "ID", "count": 9})
        self.assertEqual(v2["fires_72h"], v1["fires_72h"])
        self.assertIn(f"FIRMS unavailable, using detections from {stamp}", "\n".join(self.lines))

    def assert_published_without_fires(self, **kwargs):
        self.assertEqual(self.run_it(firms_down=True, **kwargs), 0)
        v1, v2 = self.read(self.out), self.read(self.out2)
        self.assertEqual(v1["fires_72h"], [])
        self.assertIsNone(v1["sources"]["firms_updated_utc"])
        self.assertIsNone(v2["sources"]["firms_updated_utc"])
        self.assertEqual((v2["fires_72h"], v2["fires_daily"], v2["fire_bins"]), ([], [], []))
        for cell in v2["cells"]:
            self.assertEqual(cell["drivers"]["path_fire_count_72h"], 0)
            self.assertIsNone(cell["drivers"]["path_fire_province"])

    def test_firms_down_without_a_saved_copy_publishes_no_fires(self):
        self.assert_published_without_fires()

    def test_firms_down_with_a_stale_saved_copy_publishes_no_fires(self):
        self.assertEqual(self.run_it(), 0)
        self.assert_published_without_fires(now=NOW + timedelta(hours=25))

    def read(self, path):
        with open(path) as fh:
            return json.load(fh)

    def raw(self, *paths):
        out = []
        for path in paths:
            with open(path, "rb") as fh:
                out.append(fh.read())
        return tuple(out)

    def test_both_files_written_and_v2_follows_the_contract(self):
        self.assertEqual(self.run_it(nea=nea_body()), 0)
        v1, v2 = self.read(self.out), self.read(self.out2)
        self.assertEqual((v1["version"], v2["version"]), (1, 2))
        self.assertEqual(os.path.getsize(self.out2) < 500_000, True)
        self.assertNotIn("country", v1["towns"][0])
        self.assertEqual({t["country"] for t in v2["towns"]}, {"MY", "SG"})
        self.assertIn("nea", v2)
        self.assertEqual(v2["sources"]["nea_updated_utc"], v2["nea"]["updated_utc"])
        cell = v2["cells"][0]
        self.assertEqual(len(cell["path48h"]), 17)
        self.assertEqual(cell["drivers"]["wind850_today"]["from_deg"], 307)
        self.assertEqual(cell["drivers"]["rain_tonight_mm"], 6.0)

    def test_v2_cells_are_v1_cells_with_the_same_values(self):
        self.run_it(nea=nea_body())
        v1, v2 = self.read(self.out), self.read(self.out2)
        by_key = {(c["i"], c["j"]): c for c in v2["cells"]}
        for c in v1["cells"]:
            v2c = by_key[(c["i"], c["j"])]
            same = {k: v2c[k] for k in c}
            same["now"] = {k: v for k, v in same["now"].items() if k != "weather"}  # added in v1.4
            self.assertEqual(same, c)

    def test_sg_only_on_cells_near_singapore(self):
        self.run_it(nea=nea_body())
        cells = self.read(self.out2)["cells"]
        with_sg = [(c["i"], c["j"]) for c in cells if "sg" in c]
        self.assertTrue(with_sg)
        for i, j in with_sg:
            lat, lon = gridmod.cell_center(i, j)
            self.assertTrue(0.7 < lat < 1.9 and 103.3 < lon < 104.4, (lat, lon))
        kl = next(c for c in cells if (c["i"], c["j"]) == (6, 5))
        self.assertNotIn("sg", kl)
        sg = next(c for c in cells if "sg" in c)["sg"]
        self.assertEqual(sg["tomorrow_psi"], singapore.pm25_to_psi(20.0))

    def test_nea_failure_omits_nea_but_the_job_succeeds(self):
        self.assertEqual(self.run_it(nea=None), 0)
        v2 = self.read(self.out2)
        self.assertNotIn("nea", v2)
        self.assertNotIn("nea_updated_utc", v2["sources"])
        self.assertTrue(any("omitting 'nea'" in line for line in self.lines))

    def test_second_run_downloads_no_gfs_bundle_again(self):
        self.run_it(nea=nea_body())
        first = len(self.calls)
        self.run_it(nea=nea_body())
        self.assertGreater(first, 0)
        self.assertEqual(len(self.calls), first)

    def run_with_broken_v2(self, previous):
        """Rerun with failing GFS 850 bundles; `previous` is what the Pages URL serves."""
        def broken(*a, **k):
            raise PipelineError("GFS down")

        def urlopen(req, timeout=None):
            if "greg-celestara.github.io" in req.full_url:
                if previous is None:
                    raise OSError("Pages down")
                return FakeResponse(previous)
            return fake_urlopen(self.FIRES)(req)
        with tempfile.TemporaryDirectory() as fresh:
            return run_job(ENV, out=self.out, out_v2=self.out2, cache_dir=os.path.join(
                self.root, "cache"), gfs_cache_dir=fresh, now_fn=lambda: NOW,
                log=self.lines.append, retrieve=fake_retrieve, fetch_wind=fake_wind,
                fetch_bundle=broken, http={"urlopen": urlopen, "sleep": lambda s: None})

    def test_v2_failure_still_publishes_v1_and_restores_the_previous_v2(self):
        self.run_it(nea=nea_body())
        v1_before, v2_before = self.raw(self.out, self.out2)
        os.remove(self.out)
        os.remove(self.out2)
        code = self.run_with_broken_v2(v2_before.decode())
        self.assertEqual(code, 0)
        self.assertEqual(self.raw(self.out, self.out2), (v1_before, v2_before))
        self.assertTrue(any("v2 failed" in line for line in self.lines))
        self.assertTrue(any("restored the previously published v2" in line
                            for line in self.lines))

    def test_v2_failure_and_no_previous_v2_gives_v1_only(self):
        self.run_it(nea=nea_body())
        v1_before = self.raw(self.out)[0]
        os.remove(self.out)
        os.remove(self.out2)
        self.assertEqual(self.run_with_broken_v2(None), 0)
        self.assertEqual(self.raw(self.out)[0], v1_before)
        self.assertFalse(os.path.exists(self.out2))
        self.assertTrue(any("publishing no v2 file" in line for line in self.lines))

    def test_an_invalid_previous_v2_is_not_published(self):
        self.run_it(nea=nea_body())
        os.remove(self.out2)
        self.assertEqual(self.run_with_broken_v2("not json"), 0)
        self.assertTrue(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out2))

    def test_v1_failure_still_exits_1_and_skips_v2(self):
        def no_cams(run, max_lead_h, target, key):
            raise PipelineError("ADS down")
        self.out = os.path.join(self.root, "v1", "hazemy.json")
        self.out2 = os.path.join(self.root, "v2", "hazemy.json")
        code = run_job(ENV, out=self.out, out_v2=self.out2, cache_dir=os.path.join(
            self.root, "cache"), gfs_cache_dir=os.path.join(self.root, "gfs"),
            now_fn=lambda: NOW, log=self.lines.append, retrieve=no_cams, fetch_wind=fake_wind,
            fetch_bundle=fake_bundle_factory(self.calls),
            http={"urlopen": fake_urlopen(HEADER), "sleep": lambda s: None})
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.out) or os.path.exists(self.out2))

    def test_trajectory_wind_reaches_the_generation_hour_with_forecast_steps(self):
        self.run_it(nea=nea_body())
        newest = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
        steps = {fhr for cycle, fhr, kind in self.calls if cycle == newest and kind == "w850"}
        self.assertTrue({0, 3, 6, 9, 12} <= steps, steps)  # start 10:00 UTC = f010 -> up to f012
        self.assertTrue(any("paths start 2026-10-19 10 UTC" in line for line in self.lines))

    def test_fire_fields_follow_the_detections(self):
        fires = HEADER + row(3.1, 101.6) * 4
        self.run_it(csv_text=fires, nea=nea_body())
        v2 = self.read(self.out2)
        self.assertTrue(any(c["drivers"]["path_fire_count_72h"] > 0 for c in v2["cells"]))
        self.assertTrue(v2["fire_bins"])
        self.assertEqual(v2["fires_daily"][0]["province"], v2["fires_72h"][0]["province"])


class ValidationV2(unittest.TestCase):
    def doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = JobV2()
            case.root, case.calls, case.lines = tmp, [], []
            case.run_it(nea=nea_body())
            return case.read(case.out2)

    def test_size_limit_is_enforced(self):
        doc = self.doc()
        output_v2.validate(doc)
        doc["fire_bins"] = [[0.125, 100.125, 1]] * 1500
        doc["cells"] = doc["cells"] * 40
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_bin_cap_is_enforced(self):
        doc = self.doc()
        doc["fire_bins"] = [[0.125, 100.125, 1]] * 1501
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_fire_fields_must_be_null_when_count_is_zero(self):
        doc = self.doc()
        d = doc["cells"][0]["drivers"]
        d["path_fire_count_72h"], d["path_fire_province"] = 0, "Riau"
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_path_outside_the_box_is_rejected(self):
        doc = self.doc()
        doc["cells"][0]["path48h"][3] = [0.0, 80.0]
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_v1_validator_still_rejects_v2(self):
        with self.assertRaises(PipelineError):
            output.validate(self.doc())


if __name__ == "__main__":
    unittest.main()
