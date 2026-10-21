"""End-to-end behaviour of the job with fake network boundaries and synthetic GRIB files."""
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

import numpy as np

from hazemy import compute, output
from hazemy import grid as gridmod
from hazemy.common import PipelineError
from hazemy.job import run_job
from tests.gribmaker import PM2P5_PARAM_ID, make_message
from tests.test_data_sources import HEADER, row

NOW = datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc)
ENV = {"ADS_API_KEY": "ads-secret-123", "FIRMS_MAP_KEY": "firms-secret-456"}
PM = 20.0  # ug/m3 everywhere -> API 61, band 2


def fake_retrieve(run, max_lead_h, target, key):
    """Writes a CAMS-shaped GRIB with constant PM2.5 for every 3-hourly lead."""
    date = int(f"{run:%Y%m%d}")
    with open(target, "wb") as fh:
        for lead in range(0, max_lead_h + 1, 3):
            fh.write(make_message(np.full((21, 52), PM * 1e-9), lat_first=8.0, lon_first=99.2,
                                  di=0.4, dj=0.4, param_id=PM2P5_PARAM_ID, date=date, step=lead))


def fake_wind(cycle, fhr, budget, log=print):
    """Constant 5 m/s wind blowing toward the south, i.e. from the north."""
    def msg(value):
        return make_message(np.full((21, 61), value), lat_first=10.0, lon_first=95.0, di=0.5,
                            dj=0.5, param_id=165)
    return {"UGRD": msg(0.0), "VGRD": msg(-5.0)}


class FakeResponse:
    def __init__(self, text, status=200):
        self.body, self.status = text.encode(), status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def fake_urlopen(csv_text):
    return lambda req, timeout=None: FakeResponse(csv_text)


class JobTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = os.path.join(self._dir.name, "site", "v1", "hazemy.json")
        self.cache = os.path.join(self._dir.name, "cache")
        self.lines = []
        self.now = NOW

    def run_it(self, csv_text=HEADER, retrieve=fake_retrieve, env=ENV):
        http = {"urlopen": fake_urlopen(csv_text), "sleep": lambda s: None}
        return run_job(env, out=self.out, cache_dir=self.cache, now_fn=lambda: self.now,
                       log=self.lines.append, retrieve=retrieve, fetch_wind=fake_wind, http=http)

    def read(self):
        with open(self.out) as fh:
            return json.load(fh)


class Success(JobTest):
    def test_file_follows_the_contract(self):
        fires = HEADER + row(0.5, 101.5) * 3 + row(2.0, 112.5)
        self.assertEqual(self.run_it(fires), 0)
        doc = self.read()
        self.assertEqual(list(doc), ["version", "generated_utc", "sources", "grid", "cells",
                                     "fires_72h", "towns"])
        self.assertEqual(doc["grid"], {"lat0": 0.8, "lon0": 99.6, "step": 0.4, "nlat": 17,
                                       "nlon": 50})
        self.assertEqual(doc["sources"]["cams_run_utc"], "2026-10-19T00:00:00Z")
        self.assertEqual(doc["sources"]["gfs_run_utc"], "2026-10-19T00:00:00Z")
        self.assertEqual(doc["sources"]["attribution"],
                         "Contains modified Copernicus Atmosphere Monitoring Service information "
                         "2026; NASA FIRMS; NOAA GFS")
        # The fake serves the same CSV for each of the three satellites, so counts triple.
        self.assertEqual(doc["fires_72h"], [{"province": "Riau", "country": "ID", "count": 9},
                                            {"province": "Sarawak", "country": "MY", "count": 3}])

    def test_cell_values_from_constant_fields(self):
        self.run_it()
        cell = self.read()["cells"][0]
        self.assertEqual(cell["now"], {"pm25_24h": 20.0, "api": 61, "band": 2})
        self.assertEqual(cell["tomorrow"], {"api": 61, "api_min": 61, "api_max": 61, "band": 2})
        self.assertEqual(cell["wind_today"], {"from_deg": 0, "from_compass": "N", "speed_kmh": 18})

    def test_empty_firms_result_gives_an_empty_list(self):
        self.assertEqual(self.run_it(HEADER), 0)
        self.assertEqual(self.read()["fires_72h"], [])

    def test_second_run_reuses_the_cached_download(self):
        self.run_it()
        calls = []

        def counting(*args):
            calls.append(args)
            fake_retrieve(*args)
        self.assertEqual(self.run_it(retrieve=counting), 0)
        self.assertEqual(calls, [])

    def test_previous_run_is_requested_because_windows_start_before_the_run(self):
        runs = []

        def spy(run, lead, target, key):
            runs.append((run.day, lead))
            fake_retrieve(run, lead, target, key)
        self.run_it(retrieve=spy)
        self.assertEqual(runs, [(18, 24), (19, 111)])

    def test_late_run_asks_for_tomorrow_and_reuses_the_cache_at_another_hour(self):
        late = datetime(2026, 10, 19, 16, 20, tzinfo=timezone.utc)
        later = datetime(2026, 10, 19, 21, 20, tzinfo=timezone.utc)
        calls = []

        def spy(run, lead, target, key):
            calls.append((run.day, lead))
            fake_retrieve(run, lead, target, key)
        self.now = late
        self.assertEqual(self.run_it(retrieve=spy), 0)
        # 16:20 UTC is already 20 Oct in MYT, so "tomorrow" is MYT 21 Oct (16:00 UTC on the 20th to 16:00 on the 21st).
        self.assertEqual(self.read()["cells"][0]["tomorrow"]["band"], 2)
        steps = compute.tomorrow_steps(late)
        self.assertEqual((steps[0], steps[-1]), (datetime(2026, 10, 20, 18, tzinfo=timezone.utc),
                                                 datetime(2026, 10, 21, 15, tzinfo=timezone.utc)))
        self.assertEqual(compute.lead_hours(datetime(2026, 10, 19, tzinfo=timezone.utc), late), 63)
        self.assertEqual(calls, [(18, 24), (19, 111)])
        self.now = later
        self.assertEqual(self.run_it(retrieve=spy), 0)
        self.assertEqual(calls, [(18, 24), (19, 111)])


class Failure(JobTest):
    def seed_published(self):
        os.makedirs(os.path.dirname(self.out))
        with open(self.out, "w") as fh:
            fh.write('{"last":"good"}')

    def test_failed_cams_download_exits_nonzero_and_keeps_the_published_file(self):
        self.seed_published()

        def failing(run, lead, target, key):
            raise RuntimeError(f"ADS refused key {key}")
        self.assertEqual(self.run_it(retrieve=failing), 1)
        with open(self.out) as fh:
            self.assertEqual(fh.read(), '{"last":"good"}')
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_failure_without_a_published_file_creates_none(self):
        def failing(run, lead, target, key):
            raise RuntimeError("down")
        self.assertEqual(self.run_it(retrieve=failing), 1)
        self.assertFalse(os.path.exists(self.out))

    def test_secrets_never_reach_the_log(self):
        def failing(run, lead, target, key):
            raise RuntimeError(f"ADS refused key {key}")
        self.run_it(retrieve=failing)
        log = "\n".join(self.lines)
        self.assertIn("ADS refused key ***", log)
        self.assertNotIn(ENV["ADS_API_KEY"], log)

    def test_missing_key_is_reported_before_any_download(self):
        self.assertEqual(self.run_it(env={"ADS_API_KEY": "x"}), 1)
        self.assertIn("FIRMS_MAP_KEY", "\n".join(self.lines))

    def test_firms_error_page_fails_the_run_and_keeps_the_file(self):
        self.seed_published()
        self.assertEqual(self.run_it("Invalid MAP_KEY."), 1)
        with open(self.out) as fh:
            self.assertEqual(fh.read(), '{"last":"good"}')

    def test_gfs_failing_for_both_cycles_exits_nonzero_and_keeps_the_file(self):
        self.seed_published()

        def no_gfs(cycle, fhr, budget, log=print):
            raise PipelineError(f"GFS {cycle:%H}Z not on the bucket")
        http = {"urlopen": fake_urlopen(HEADER), "sleep": lambda s: None}
        code = run_job(ENV, out=self.out, cache_dir=self.cache, now_fn=lambda: NOW,
                       log=self.lines.append, retrieve=fake_retrieve, fetch_wind=no_gfs, http=http)
        self.assertEqual(code, 1)
        self.assertIn("No usable GFS cycle", "\n".join(self.lines))
        with open(self.out) as fh:
            self.assertEqual(fh.read(), '{"last":"good"}')

    def test_incomplete_cams_timeline_fails_the_run(self):
        def short(run, lead, target, key):
            fake_retrieve(run, 6, target, key)
        self.assertEqual(self.run_it(retrieve=short), 1)
        self.assertIn("lacks", "\n".join(self.lines))


class FirmsSaved(JobTest):
    def test_success_saves_the_detections(self):
        self.run_it(HEADER + row(0.5, 101.5))
        with open(os.path.join(self.cache, "firms_last.json")) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["fetched_utc"], "2026-10-19T10:30:00Z")
        self.assertEqual(len(saved["detections"]), 3)
        self.assertEqual(self.read()["sources"]["firms_updated_utc"], "2026-10-19T10:30:00Z")

    def run_with(self, urlopen, retrieve=fake_retrieve, now=None):
        http = {"urlopen": urlopen, "sleep": lambda s: None}
        self.out_v2 = os.path.join(self._dir.name, "site", "v2", "hazemy.json")
        return run_job(ENV, out=self.out, cache_dir=self.cache,
                       now_fn=lambda: now or self.now, log=self.lines.append,
                       retrieve=retrieve, fetch_wind=fake_wind, http=http)

    @staticmethod
    def unreachable(only=None, body=HEADER):
        def urlopen(req, timeout=None):
            if only is None or only in req.full_url:
                raise urllib.error.URLError("Network is unreachable")
            return FakeResponse(body)
        return urlopen

    def test_cams_failure_stays_fatal_even_with_firms_down(self):
        def failing(run, lead, target, key):
            raise RuntimeError("ADS down")
        self.assertEqual(self.run_with(self.unreachable(), retrieve=failing), 1)
        self.assertFalse(os.path.exists(self.out))

    def test_firms_rejected_key_is_fatal_and_writes_nothing(self):
        def rejected(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        self.assertEqual(self.run_with(rejected), 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out_v2))

    def test_one_product_exhausting_retries_uses_the_saved_copy_untouched(self):
        self.run_it(HEADER + row(0.5, 101.5))
        path = os.path.join(self.cache, "firms_last.json")
        with open(path) as fh:
            before = fh.read()
        later = datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(self.run_with(self.unreachable("VIIRS_NOAA21_NRT"), now=later), 0)
        with open(path) as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.read()["sources"]["firms_updated_utc"], "2026-10-19T10:30:00Z")

    def test_corrupt_or_empty_saved_copy_means_no_fires(self):
        os.makedirs(self.cache)
        for content in ("", "{not json", '{"fetched_utc": 5}'):
            with open(os.path.join(self.cache, "firms_last.json"), "w") as fh:
                fh.write(content)
            self.assertEqual(self.run_with(self.unreachable()), 0)
            doc = self.read()
            self.assertEqual(doc["fires_72h"], [])
            self.assertIsNone(doc["sources"]["firms_updated_utc"])


class Validation(unittest.TestCase):
    def doc(self, **override):
        cells = gridmod.land_cells(gridmod.malaysia_land(gridmod.load_provinces()))[:2]
        air = [{"now": {"pm25_24h": 20.0, "api": 61, "band": 2},
                "tomorrow": {"api": 61, "api_min": 60, "api_max": 62, "band": 2}}] * 2
        wind = [{"from_deg": 0, "from_compass": "N", "speed_kmh": 3}] * 2
        args = dict(generated=NOW, cams_run=NOW, firms_fetched=NOW, gfs_run=NOW, cells=cells,
                    air=air, wind=wind, fires=[], towns=[{"name": "A", "lat": 1.0, "lon": 100.0}])
        args.update(override)
        return output.assemble(**args)

    def test_valid_document_passes_and_is_compact(self):
        body = output.validate(self.doc())
        self.assertNotIn(b": ", body)
        self.assertLess(len(body), output.MAX_BYTES)

    def test_file_size_limit_is_enforced(self):
        towns = [{"name": "T" * 40, "lat": 1.0, "lon": 100.0}] * 8000
        with self.assertRaises(PipelineError) as ctx:
            output.validate(self.doc(towns=towns))
        self.assertIn("500000", str(ctx.exception))

    def test_band_outside_range_is_rejected(self):
        doc = self.doc()
        doc["cells"][0]["now"]["band"] = 6
        with self.assertRaises(PipelineError):
            output.validate(doc)

    def test_unsorted_fires_are_rejected(self):
        fires = [{"province": "A", "country": "ID", "count": 1},
                 {"province": "B", "country": "ID", "count": 5}]
        with self.assertRaises(PipelineError):
            output.validate(self.doc(fires=fires))

    def test_nan_cannot_be_written(self):
        doc = self.doc()
        doc["cells"][0]["now"]["pm25_24h"] = float("nan")
        with self.assertRaises((PipelineError, ValueError)):
            output.validate(doc)


class ComputeBehaviour(unittest.TestCase):
    def test_tomorrow_min_max_follow_rolling_windows(self):
        times = compute.required_times(NOW)
        # PM2.5 rises 1 ug/m3 per 3 h step; later windows must therefore have a higher API.
        series = {t: np.array([10.0 + k]) for k, t in enumerate(times)}
        tomorrow = compute.cell_air(series, NOW)[0]["tomorrow"]
        self.assertLess(tomorrow["api_min"], tomorrow["api"])
        # The window ending at the day's last step is the day itself, the highest when rising.
        self.assertEqual(tomorrow["api"], tomorrow["api_max"])

    def test_towns_file_is_usable(self):
        from hazemy.job import load_towns
        towns = load_towns()
        self.assertTrue(60 <= len(towns) <= 120, len(towns))
        names = {t["name"] for t in towns}
        for capital in ("Kuala Lumpur", "Kuching", "Kota Kinabalu", "Kangar", "Putrajaya"):
            self.assertIn(capital, names)
        self.assertTrue(all(0 < t["lat"] < 8 and 99 < t["lon"] < 120 for t in towns))


if __name__ == "__main__":
    unittest.main()
