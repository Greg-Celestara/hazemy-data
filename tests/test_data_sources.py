"""Cell filtering, CAMS and GFS decoding, FIRMS parsing and province counting."""
import os
import unittest
from datetime import datetime, timezone

import numpy as np
from shapely.geometry import box

from hazemy import cams, firms, gfs
from hazemy import grid as gridmod
from hazemy.common import Budget, PipelineError
from tests.gribmaker import PM2P5_PARAM_ID, make_message

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class CellFilter(unittest.TestCase):
    def setUp(self):
        # A 1-degree square of "land" around lat 3-4 N, lon 101-102 E, in km units.
        k = gridmod.KM_PER_DEG
        self.land = box(101 * k, 3 * k, 102 * k, 4 * k)

    def test_cells_over_and_near_land_are_kept(self):
        cells = {(c[2], c[3]) for c in gridmod.land_cells(self.land)}
        self.assertIn((3.2, 101.2), cells)      # inside
        self.assertIn((4.0, 102.0), cells)      # on the corner
        self.assertIn((4.4, 101.6), cells)      # 0.4 deg (44 km) north of the edge

    def test_cells_beyond_50_km_are_dropped(self):
        cells = {(c[2], c[3]) for c in gridmod.land_cells(self.land)}
        self.assertNotIn((4.8, 101.6), cells)   # 0.8 deg = 89 km away
        self.assertNotIn((1.2, 110.0), cells)

    def test_radius_edge(self):
        near = gridmod.land_cells(self.land, radius_km=44.0)   # 0.4 deg = 44.4 km
        self.assertNotIn((4.4, 101.6), {(c[2], c[3]) for c in near})

    def test_real_province_file_gives_a_plausible_cell_set(self):
        provinces = gridmod.load_provinces()
        cells = gridmod.land_cells(gridmod.malaysia_land(provinces))
        coords = {(c[2], c[3]) for c in cells}
        self.assertIn((3.2, 101.6), coords)        # Kuala Lumpur area
        self.assertNotIn((7.2, 99.6), coords)      # open sea off southern Thailand
        self.assertTrue(100 < len(cells) < 600, len(cells))
        self.assertTrue(all(0 <= c[0] < 17 and 0 <= c[1] < 50 for c in cells))


class Cams(unittest.TestCase):
    def write(self, name, raw):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_real_fixture_decodes_with_micrograms_and_valid_times(self):
        fields = cams.decode_grib(os.path.join(FIXTURES, "pm2p5_20261013_00z_lt0_24.grib"))
        self.assertIn(utc(2026, 10, 13, 0), fields)
        field, geometry = fields[utc(2026, 10, 13, 0)]
        self.assertEqual(field.shape, (geometry["nj"], geometry["ni"]))
        self.assertTrue(0 < np.nanmean(field) < 500)   # ug/m3, not kg/m3

    def test_synthetic_message_values_and_step(self):
        raw = make_message(np.full((21, 50), 25e-9), lat_first=8.0, lon_first=99.2, di=0.4,
                           dj=0.4, param_id=PM2P5_PARAM_ID, step=6)
        fields = cams.decode_grib(self.write("a.grib", raw))
        self.assertEqual(list(fields), [utc(2026, 10, 19, 6)])
        self.assertAlmostEqual(float(fields[utc(2026, 10, 19, 6)][0][0, 0]), 25.0, places=3)

    def test_wrong_parameter_is_rejected(self):
        raw = make_message(np.zeros((21, 50)), lat_first=8.0, lon_first=99.2, di=0.4, dj=0.4,
                           param_id=165)
        with self.assertRaises(PipelineError):
            cams.decode_grib(self.write("b.grib", raw))

    def test_sampling_picks_the_grid_point_of_each_cell(self):
        values = np.arange(21 * 52, dtype=float).reshape(21, 52)
        geometry = {"ni": 52, "nj": 21, "lat_first": 8.0, "lon_first": 99.2, "di": 0.4,
                    "dj": 0.4, "south_first": False}
        got = cams.sample_cells(values, geometry, np.array([0.8, 7.2]), np.array([99.6, 119.2]))
        # lat 0.8 is row (8.0-0.8)/0.4 = 18, lon 99.6 is col 1; lat 7.2 is row 2, lon 119.2 col 50.
        self.assertEqual(list(got), [18 * 52 + 1, 2 * 52 + 50])

    def test_sampling_off_grid_raises(self):
        geometry = {"ni": 50, "nj": 21, "lat_first": 8.0, "lon_first": 99.25, "di": 0.4,
                    "dj": 0.4, "south_first": False}
        with self.assertRaises(PipelineError):
            cams.sample_cells(np.zeros((21, 50)), geometry, np.array([0.8]), np.array([99.6]))

    def test_sampling_outside_download_raises(self):
        geometry = {"ni": 10, "nj": 10, "lat_first": 8.0, "lon_first": 99.2, "di": 0.4,
                    "dj": 0.4, "south_first": False}
        with self.assertRaises(PipelineError):
            cams.sample_cells(np.zeros((10, 10)), geometry, np.array([0.8]), np.array([99.6]))

    def test_newer_run_wins_overlapping_valid_times(self):
        merged = cams.merge_runs([{1: "old", 2: "old"}, {2: "new"}])
        self.assertEqual(merged, {1: "old", 2: "new"})

    def test_expected_run_follows_the_publish_lag(self):
        self.assertEqual(cams.expected_run(utc(2026, 10, 19, 9, 59)), utc(2026, 10, 18))
        self.assertEqual(cams.expected_run(utc(2026, 10, 19, 10, 0)), utc(2026, 10, 19))

    def test_download_over_budget_is_refused_and_leaves_no_file(self):
        def retrieve(run, lead, target, key):
            with open(target, "wb") as fh:
                fh.write(b"x" * 100)
        with self.assertRaises(PipelineError):
            cams.cached_grib(utc(2026, 10, 19), 3, self.tmp, "k", Budget(10), retrieve=retrieve,
                             log=lambda _: None)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".grib")], [])


class Gfs(unittest.TestCase):
    def test_idx_selects_10m_wind_with_byte_ranges(self):
        with open(os.path.join(FIXTURES, "gfs_20260920_f000.idx")) as fh:
            messages = gfs.select_wind(gfs.parse_idx(fh.read()))
        self.assertEqual([(m.var, m.start, m.end) for m in messages],
                         [("UGRD", 416890557, 417860190), ("VGRD", 417860191, 418811800)])

    def test_idx_without_the_wind_entry_raises(self):
        with self.assertRaises(PipelineError):
            gfs.select_wind(gfs.parse_idx("1:0:d=2026092000:TMP:2 m above ground:anl:\n"
                                          "2:100:d=2026092000:PRES:surface:anl:\n"))

    def test_plan_uses_each_past_times_own_cycle(self):
        newest = utc(2026, 10, 19, 0)
        plan = gfs.plan_steps([utc(2026, 10, 18, 18), utc(2026, 10, 18, 21), utc(2026, 10, 19, 0),
                               utc(2026, 10, 19, 6)], newest)
        self.assertEqual([(c.hour, f) for _, c, f in plan], [(18, 0), (18, 3), (0, 0), (0, 6)])

    def test_latest_cycle_respects_the_publish_lag(self):
        self.assertEqual(gfs.latest_cycle(utc(2026, 10, 19, 10, 20)), utc(2026, 10, 19, 0))
        self.assertEqual(gfs.latest_cycle(utc(2026, 10, 19, 11, 0)), utc(2026, 10, 19, 6))

    def test_bilinear_interpolates_between_grid_points(self):
        field = np.array([[0.0, 10.0], [20.0, 30.0]])
        geometry = {"ni": 2, "nj": 2, "lat_first": 1.0, "lon_first": 100.0, "di": 1.0,
                    "dj": 1.0, "south_first": False}
        got = gfs.bilinear(field, geometry, np.array([0.5, 1.0]), np.array([100.5, 101.0]))
        self.assertEqual(list(got), [15.0, 10.0])

    def test_bilinear_outside_the_grid_raises(self):
        geometry = {"ni": 2, "nj": 2, "lat_first": 1.0, "lon_first": 100.0, "di": 1.0,
                    "dj": 1.0, "south_first": False}
        with self.assertRaises(PipelineError):
            gfs.bilinear(np.zeros((2, 2)), geometry, np.array([5.0]), np.array([100.5]))


HEADER = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
          "confidence,version,bright_ti5,frp,daynight\n")


def row(lat, lon, date="2026-10-19", time="530", conf="n"):
    return f"{lat},{lon},330.0,0.4,0.4,{date},{time},N,VIIRS,{conf},2.0NRT,290.0,5.0,D\n"


class Fires(unittest.TestCase):
    NOW = utc(2026, 10, 19, 10, 0)

    def parse(self, text):
        return firms.parse_csv(text, "TEST", self.NOW)

    def test_header_only_means_no_fires(self):
        self.assertEqual(self.parse(HEADER), [])
        self.assertEqual(firms.count_by_province([], gridmod.load_provinces()), [])

    def test_low_confidence_and_old_rows_are_dropped(self):
        text = (HEADER + row(1, 100, conf="l") + row(2, 101) + row(3, 102, date="2026-10-16")
                + row(4, 103, date="2026-10-16", time="1100"))
        # 2026-10-16 10:00 is exactly 72 h before NOW: not inside the window; 11:00 is.
        self.assertEqual(self.parse(text), [(2.0, 101.0), (4.0, 103.0)])

    def test_acq_time_without_leading_zeros(self):
        self.assertEqual(self.parse(HEADER + row(2, 101, time="5")), [(2.0, 101.0)])  # 00:05

    def test_error_page_with_http_200_raises(self):
        with self.assertRaises(PipelineError):
            self.parse("Invalid MAP_KEY.")

    def test_counts_by_province_sorted_descending_top_n(self):
        provinces = gridmod.load_provinces()
        sarawak, riau = (2.0, 112.5), (0.5, 101.5)
        points = [sarawak] * 2 + [riau] * 5 + [(-90.0, 0.0)]
        result = firms.count_by_province(points, provinces)
        self.assertEqual([(r["province"], r["country"], r["count"]) for r in result],
                         [("Riau", "ID", 5), ("Sarawak", "MY", 2)])

    def test_only_five_provinces_are_returned(self):
        provinces = [(f"P{k}", "IDN", box(k, 0, k + 0.5, 1)) for k in range(8)]
        points = [(0.5, k + 0.25) for k in range(8) for _ in range(k + 1)]
        result = firms.count_by_province(points, provinces)
        self.assertEqual([r["count"] for r in result], [8, 7, 6, 5, 4])


if __name__ == "__main__":
    unittest.main()
