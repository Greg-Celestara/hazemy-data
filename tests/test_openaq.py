"""OpenAQ community sensors: filters, pairing, bias.csv storage and the job's failure handling.

The network is faked everywhere and no API key is needed.
"""
import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy import openaq, output_v2
from hazemy.common import Budget
from hazemy.job import run_job
from tests.test_data_sources import HEADER
from tests.test_job import NOW, FakeResponse, fake_retrieve, fake_urlopen, fake_wind
from tests.test_v2 import fake_bundle_factory

KEY = "openaq-secret-789"
ENV = {"ADS_API_KEY": "ads-secret-123", "FIRMS_MAP_KEY": "firms-secret-456",
       "OPENAQ_API_KEY": KEY}
STEP = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)  # NOW is 10:30, so 09:00 is the latest
KL = (3.139, 101.687)


def at(hour, minute=0):
    return datetime(2026, 10, 19, hour, minute, tzinfo=timezone.utc)


def location(sensor_id, model="O-1PST", lat=KL[0], lon=KL[1], name="Unit"):
    instruments = [{"name": f"AirGradient {model}"}] if model else []
    return {"id": sensor_id + 1000, "name": name, "coordinates": {"latitude": lat,
                                                                  "longitude": lon},
            "provider": {"name": "AirGradient"}, "owner": {"name": "Owner"},
            "instruments": instruments, "datetimeLast": {"utc": "2026-10-19T10:00:00Z"},
            "sensors": [{"id": sensor_id, "parameter": {"id": 2, "name": "pm25"}},
                        {"id": sensor_id + 5000, "parameter": {"id": 1, "name": "pm10"}}]}


def series_of(*pairs):
    return [(at(h, m), v) for (h, m), v in pairs]


GOOD = series_of(((7, 0), 100.0), ((8, 0), 10.0), ((9, 0), 14.0), ((10, 0), 12.0))
STUCK = series_of(((8, 0), 9.0), ((9, 0), 9.0), ((10, 0), 9.0))
ZERO = series_of(((8, 0), 5.0), ((9, 0), 6.0), ((10, 0), 0.0))
STALE = series_of(((6, 0), 5.0), ((7, 0), 6.0), ((8, 0), 7.0))
HIGH = series_of(((8, 0), 5.0), ((9, 0), 6.0), ((10, 0), 1500.0))


class FakeOpenAQ:
    """Fake urlopen for OpenAQ and the published bias.csv; everything else is FIRMS."""

    def __init__(self, locations=(), readings=None, fail=None, published=None):
        self.locations, self.readings = list(locations), readings or {}
        self.fail, self.published = fail, published
        self.keys, self.urls = [], []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "api.openaq.org" in url:
            self.keys.append(req.get_header("X-api-key"))
            return self.openaq(url)
        if url == openaq.PUBLISHED_BIAS_URL:
            return self.published_copy(url)
        return fake_urlopen(HEADER)(req)

    def openaq(self, url):
        if self.fail == "500":
            raise urllib.error.HTTPError(url, 500, "boom", {}, None)
        if self.fail == "timeout":
            raise TimeoutError("timed out")
        if self.fail == "json":
            return FakeResponse("<html>not json")
        if "/locations" in url:
            if self.fail == "missing-key":
                return FakeResponse(json.dumps({"meta": {}}))
            return FakeResponse(json.dumps({"results": self.locations}))
        sensor_id = int(url.split("/sensors/")[1].split("/")[0])
        results = [{"value": v, "period": {"datetimeTo": {"utc": t.strftime("%Y-%m-%dT%H:%M:%SZ")}}}
                   for t, v in self.readings.get(sensor_id, [])]
        page = int(url.split("page=")[1].split("&")[0])
        results = results[(page - 1) * openaq.PAGE_LIMIT:page * openaq.PAGE_LIMIT]
        return FakeResponse(json.dumps({"results": results}))

    def published_copy(self, url):
        if self.published is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        if self.published == "down":
            raise urllib.error.HTTPError(url, 503, "down", {}, None)
        return FakeResponse(self.published)


def http_of(net):
    return {"urlopen": net, "sleep": lambda s: None}


class ModelCode(unittest.TestCase):
    def test_instrument_name_is_checked_before_the_location_name(self):
        loc = {"name": "Mont Kiara I-9PSL-DE", "instruments": [{"name": "AirGradient O-1PST"}]}
        self.assertEqual(openaq.airgradient_model(loc), "O-1PST")

    def test_location_name_is_the_fallback(self):
        self.assertEqual(openaq.airgradient_model({"name": "KL O-1PP", "instruments": []}),
                         "O-1PP")

    def test_no_model_anywhere_is_none_and_excluded(self):
        loc = {"name": "Somewhere", "instruments": [{"name": "AirGradient"}]}
        self.assertIsNone(openaq.airgradient_model(loc))
        self.assertFalse(openaq.is_outdoor(loc))

    def test_only_outdoor_models_qualify(self):
        self.assertTrue(openaq.is_outdoor({"instruments": [{"name": "O-1PST"}]}))
        self.assertFalse(openaq.is_outdoor({"instruments": [{"name": "I-9PSL-DE"}]}))


class Collect(unittest.TestCase):
    def collect(self, locations, readings):
        net = FakeOpenAQ(locations, readings)
        lines = []
        used = openaq.collect_used(KEY, NOW, Budget(), lines.append, http_of(net))
        return used, net, lines

    def test_only_the_good_sensor_is_used(self):
        locations = [location(1), location(2, model="I-9PSL-DE"), location(3, model=None),
                     location(4), location(5), location(6), location(7)]
        readings = {1: GOOD, 2: GOOD, 3: GOOD, 4: ZERO, 5: STUCK, 6: STALE, 7: HIGH}
        used, net, _ = self.collect(locations, readings)
        self.assertEqual([s["sensor_id"] for s in used], [1])
        self.assertEqual(used[0]["pm25"], 12.0)
        self.assertEqual(used[0]["contributor"], "Owner")
        self.assertTrue(all(k == KEY for k in net.keys))

    def test_indoor_and_unmodelled_units_are_never_asked_for_readings(self):
        _, net, _ = self.collect([location(2, model="I-9PSL-DE"), location(3, model=None)], {})
        self.assertFalse(any("/sensors/" in u for u in net.urls))

    def test_location_outside_the_box_is_dropped(self):
        used, _, _ = self.collect([location(1, lat=10.0)], {1: GOOD})
        self.assertEqual(used, [])

    def test_sensor_cap_is_logged(self):
        locations = [location(n) for n in range(1, 46)]
        used, net, lines = self.collect(locations, {n: GOOD for n in range(1, 46)})
        self.assertEqual(len(used), openaq.MAX_SENSORS)
        self.assertTrue(any("capped at 40 of 45" in line for line in lines))
        self.assertEqual(sum("/sensors/" in u for u in net.urls), openaq.MAX_SENSORS)

    def test_readings_are_paged_until_a_short_page(self):
        many = [(at(5, 0) + timedelta(minutes=10 * n), 5.0 + n) for n in range(5)]
        net = FakeOpenAQ([location(1)], {1: many})
        old = openaq.PAGE_LIMIT
        openaq.PAGE_LIMIT = 2
        self.addCleanup(setattr, openaq, "PAGE_LIMIT", old)
        got = openaq.fetch_readings(KEY, 1, NOW, Budget(), print, http_of(net))
        self.assertEqual(got, many)
        self.assertEqual(sum("/sensors/" in u for u in net.urls), 3)

    def test_paging_stops_at_five_pages_and_says_so(self):
        many = [(at(0, 0) + timedelta(minutes=n), 5.0 + n) for n in range(20)]
        net = FakeOpenAQ([location(1)], {1: many})
        old = openaq.PAGE_LIMIT
        openaq.PAGE_LIMIT = 2
        self.addCleanup(setattr, openaq, "PAGE_LIMIT", old)
        lines = []
        got = openaq.fetch_readings(KEY, 1, NOW, Budget(), lines.append, http_of(net))
        self.assertEqual(len(got), 10)
        self.assertTrue(any("stopped at 5 pages" in line for line in lines))

    def test_edge_values_of_the_filters(self):
        fresh = series_of(((8, 0), 1.0), ((9, 0), 0.5))
        self.assertTrue(openaq.passes_filters(fresh, NOW))
        self.assertFalse(openaq.passes_filters(series_of(((9, 0), 1.0), ((9, 30), 1.0)), NOW))
        self.assertFalse(openaq.passes_filters([], NOW))
        old = series_of(((8, 0), 1.0), ((8, 30), 2.0))  # exactly 2 h old: not under 2 h
        self.assertFalse(openaq.passes_filters(old, NOW))


class Pairing(unittest.TestCase):
    CELLS = [(0, 0, 1.0, 100.0), (0, 1, 1.0, 100.4), (1, 0, 1.4, 100.0)]

    def sensor(self, lat, lon, readings=GOOD):
        return {"sensor_id": 9, "lat": lat, "lon": lon, "provider": "AirGradient",
                "contributor": "Owner", "readings": readings}

    def series(self):
        return {STEP: np.array([10.0, 20.0, 30.0]), STEP + timedelta(hours=3): np.zeros(3)}

    def test_nearest_cell_on_a_known_grid(self):
        self.assertEqual(openaq.nearest_cell(1.05, 100.35, self.CELLS)[0], 1)
        self.assertEqual(openaq.nearest_cell(1.35, 100.02, self.CELLS)[0], 2)
        k, km = openaq.nearest_cell(1.0, 100.0, self.CELLS)
        self.assertEqual((k, round(km, 3)), (0, 0.0))

    def test_row_uses_the_cell_value_of_the_step_and_the_step_window_mean(self):
        rows = openaq.bias_rows([self.sensor(1.05, 100.35)], self.CELLS, self.series(), NOW)
        self.assertEqual(len(rows), 1)
        step, sid, prov, contrib, _, _, mean, cams, i, j, km, flags, n = rows[0]
        self.assertEqual((step, sid, prov, contrib), ("2026-10-19T09:00:00Z", "9", "AirGradient",
                                                      "Owner"))
        self.assertEqual((mean, cams, i, j, flags, n), ("12.0", "20.0", "0", "1", "", "3"))
        self.assertLess(float(km), 20)

    def test_sensor_beyond_20_km_is_kept_and_flagged(self):
        rows = openaq.bias_rows([self.sensor(1.0, 100.8)], self.CELLS, self.series(), NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][11], "far")
        self.assertGreater(float(rows[0][10]), 20)

    def test_window_is_half_open_and_ignores_readings_outside_it(self):
        readings = series_of(((7, 29), 500.0), ((7, 30), 4.0), ((10, 29), 8.0), ((10, 30), 900.0))
        self.assertEqual(openaq.step_mean(readings, STEP), (6.0, 2))
        self.assertEqual(openaq.step_mean(series_of(((6, 0), 5.0)), STEP), (None, 0))

    def test_latest_step_is_the_newest_whose_window_has_ended(self):
        series = {at(h): None for h in (3, 6, 9, 12)}
        self.assertEqual(openaq.latest_step(at(10, 30), series), STEP)
        self.assertEqual(openaq.latest_step(at(10, 29), series), at(6))
        self.assertIsNone(openaq.latest_step(at(4, 29), series))

    def test_no_row_when_the_window_holds_no_reading(self):
        readings = series_of(((10, 0), 5.0))
        sensor = self.sensor(1.05, 100.35, readings=series_of(((10, 40), 5.0)))
        self.assertEqual(openaq.bias_rows([sensor], self.CELLS, self.series(), NOW), [])
        self.assertEqual(len(openaq.bias_rows([self.sensor(1.05, 100.35, readings)],
                                              self.CELLS, self.series(), NOW)), 1)


def row_for(step, sensor_id="1", mean="5.0"):
    return [step, sensor_id, "AirGradient", "Owner", "3.1", "101.6", mean, "20.0", "6", "5",
            "4.0", "", "3"]


class Csv(unittest.TestCase):
    def test_merge_dedupes_on_step_and_sensor_and_the_later_row_wins(self):
        old = [row_for("2026-10-19T09:00:00Z", "1", "5.0")]
        new = [row_for("2026-10-19T09:00:00Z", "1", "9.0"),
               row_for("2026-10-19T09:00:00Z", "2"), row_for("2026-10-19T12:00:00Z", "1")]
        merged = openaq.merge_rows(old, new)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0][6], "9.0")

    def test_cap_drops_the_oldest_month_first(self):
        rows = [row_for(f"2026-{m:02d}-15T09:00:00Z", str(n)) for m in (8, 9, 10)
                for n in range(100)]
        full = len(openaq.render_csv(rows).encode())
        text = openaq.cap_csv(rows, limit=full - 1)
        months = {r[0][:7] for r in openaq.parse_csv(text)}
        self.assertEqual(months, {"2026-09", "2026-10"})
        self.assertLessEqual(len(text.encode()), full - 1)

    def test_cap_keeps_everything_when_under_the_limit(self):
        rows = [row_for("2026-10-19T09:00:00Z")]
        self.assertEqual(openaq.cap_csv(rows), openaq.render_csv(rows))

    def test_cap_can_empty_a_single_oversized_month(self):
        rows = [row_for("2026-10-19T09:00:00Z", str(n)) for n in range(50)]
        text = openaq.cap_csv(rows, limit=10)
        self.assertEqual(openaq.parse_csv(text), [])

    def test_wrong_header_is_rejected(self):
        with self.assertRaises(openaq.PipelineError):
            openaq.parse_csv("<html>\n")


class Seed(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = os.path.join(self._dir.name, "site", "internal", "bias.csv")
        self.cache = os.path.join(self._dir.name, "cache")
        self.lines = []
        self.new = [row_for("2026-10-19T09:00:00Z", "7")]

    def csv_with(self, *ids):
        return openaq.render_csv([row_for("2026-10-18T09:00:00Z", i) for i in ids])

    def store(self, published, cache_text=None):
        if cache_text is not None:
            os.makedirs(self.cache)
            with open(os.path.join(self.cache, "bias.csv"), "w") as fh:
                fh.write(cache_text)
        net = FakeOpenAQ(published=published)
        openaq.store_bias(self.new, self.out, self.cache, Budget(), self.lines.append,
                          http_of(net))

    def ids(self, path):
        with open(path) as fh:
            return [r[1] for r in openaq.parse_csv(fh.read())]

    def test_published_copy_comes_first_and_both_files_are_written(self):
        self.store(self.csv_with("1"), cache_text=self.csv_with("2"))
        self.assertEqual(self.ids(self.out), ["1", "7"])
        self.assertEqual(self.ids(os.path.join(self.cache, "bias.csv")), ["1", "7"])

    def test_the_cache_wins_when_its_last_step_is_newer(self):
        newer = openaq.render_csv([row_for("2026-10-18T12:00:00Z", "2")])
        self.store(self.csv_with("1"), cache_text=newer)
        self.assertEqual(self.ids(self.out), ["2", "7"])

    def test_published_wins_a_tie(self):
        same = openaq.render_csv([row_for("2026-10-18T09:00:00Z", "2")])
        self.store(self.csv_with("1"), cache_text=same)
        self.assertEqual(self.ids(self.out), ["1", "7"])

    def test_empty_new_rows_still_publish_the_history_unchanged(self):
        self.new = []
        self.store(self.csv_with("1"))
        with open(self.out) as fh:
            self.assertEqual(fh.read(), self.csv_with("1"))

    def test_cache_is_used_when_the_published_copy_is_down(self):
        self.store("down", cache_text=self.csv_with("2"))
        self.assertEqual(self.ids(self.out), ["2", "7"])

    def test_cache_is_used_when_the_published_copy_is_not_a_bias_file(self):
        self.store("<html>oops</html>", cache_text=self.csv_with("2"))
        self.assertEqual(self.ids(self.out), ["2", "7"])

    def test_new_file_only_on_404_with_no_cache(self):
        self.store(None)
        self.assertEqual(self.ids(self.out), ["7"])

    def test_other_failure_with_no_cache_writes_nothing_and_logs_an_error(self):
        self.store("down")
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "bias.csv")))
        self.assertTrue(any(line.startswith("ERROR") for line in self.lines))

    def test_unusable_cache_after_a_blip_is_not_replaced_by_an_empty_file(self):
        self.store("down", cache_text="garbage")
        self.assertFalse(os.path.exists(self.out))


class JobSensors(unittest.TestCase):
    LOCATIONS = [location(1), location(2, model="I-9PSL-DE"), location(3, model=None),
                 location(4), location(5), location(6)]
    READINGS = {1: GOOD, 2: GOOD, 3: GOOD, 4: ZERO, 5: STUCK, 6: STALE}

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        self.paths = {"v1": os.path.join(self.root, "v1", "hazemy.json"),
                      "v2": os.path.join(self.root, "v2", "hazemy.json"),
                      "bias": os.path.join(self.root, "internal", "bias.csv")}
        self.lines = []

    def run_it(self, net, env=ENV, cache="cache"):
        return run_job(env, out=self.paths["v1"], out_v2=self.paths["v2"],
                       out_bias=self.paths["bias"], cache_dir=os.path.join(self.root, cache),
                       gfs_cache_dir=os.path.join(self.root, "gfs"), now_fn=lambda: NOW,
                       log=self.lines.append, retrieve=fake_retrieve, fetch_wind=fake_wind,
                       fetch_bundle=fake_bundle_factory([]), http=http_of(net))

    def load(self, name):
        with open(self.paths[name]) as fh:
            return json.load(fh)

    def good_net(self, **kwargs):
        return FakeOpenAQ(self.LOCATIONS, self.READINGS, **kwargs)

    def bias_rows(self):
        with open(self.paths["bias"]) as fh:
            return list(csv.DictReader(io.StringIO(fh.read())))

    def test_sensors_block_and_bias_row_are_written(self):
        self.assertEqual(self.run_it(self.good_net()), 0)
        block = self.load("v2")["sensors"]
        self.assertEqual(block["openaq_attribution"],
                         "Data provided by AirGradient via OpenAQ (Licensed under CC-BY 4.0)")
        self.assertEqual(block["openaq_updated_utc"], "2026-10-19T10:00:00Z")
        self.assertEqual(block["sensors"], [{
            "lat": KL[0], "lon": KL[1], "name": "Unit", "contributor": "Owner",
            "provider": "AirGradient", "pm25": 12.0, "measured_utc": "2026-10-19T10:00:00Z"}])
        rows = self.bias_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["step_utc"], rows[0]["sensor_mean"], rows[0]["cams_pm25"],
                          rows[0]["flags"]), ("2026-10-19T09:00:00Z", "12.0", "20.0", ""))
        self.assertTrue(os.path.exists(os.path.join(self.root, "cache", "bias.csv")))

    def test_v1_is_byte_identical_with_and_without_openaq(self):
        self.run_it(FakeOpenAQ(), env={k: v for k, v in ENV.items() if k != "OPENAQ_API_KEY"})
        with open(self.paths["v1"], "rb") as fh:
            without = fh.read()
        self.assertNotIn("sensors", self.load("v2"))
        self.run_it(self.good_net())
        with open(self.paths["v1"], "rb") as fh:
            self.assertEqual(fh.read(), without)
        self.assertNotIn("sensors", self.load("v1"))
        self.assertIn("sensors", self.load("v2"))

    HISTORY = openaq.render_csv([row_for("2026-10-18T09:00:00Z", "3")])

    def assert_bias_unchanged(self):
        with open(self.paths["bias"]) as fh:
            self.assertEqual(fh.read(), self.HISTORY)
        with open(os.path.join(self.root, "cache", "bias.csv")) as fh:
            self.assertEqual(fh.read(), self.HISTORY)

    def assert_failure_leaves_v1_and_v2(self, fail):
        self.assertEqual(self.run_it(self.good_net(fail=fail, published=self.HISTORY)), 0)
        self.assertEqual(self.load("v1")["version"], 1)
        v2 = self.load("v2")
        self.assertEqual(v2["version"], 2)
        self.assertNotIn("sensors", v2)
        self.assert_bias_unchanged()
        self.assertTrue(any("OpenAQ failed" in line for line in self.lines))

    def test_openaq_500_leaves_v1_and_v2(self):
        self.assert_failure_leaves_v1_and_v2("500")

    def test_openaq_timeout_leaves_v1_and_v2(self):
        self.assert_failure_leaves_v1_and_v2("timeout")

    def test_openaq_bad_json_leaves_v1_and_v2(self):
        self.assert_failure_leaves_v1_and_v2("json")

    def test_openaq_answer_missing_keys_leaves_v1_and_v2(self):
        self.assert_failure_leaves_v1_and_v2("missing-key")

    def test_a_malformed_reading_leaves_v1_and_v2(self):
        net = self.good_net()
        net.readings = {1: [(at(10), "n/a")]}
        net.locations = [location(1)]
        self.assertEqual(self.run_it(net), 0)
        self.assertNotIn("sensors", self.load("v2"))

    def test_no_api_key_skips_openaq_with_one_log_line(self):
        net = self.good_net(published=self.HISTORY)
        env = {k: v for k, v in ENV.items() if k != "OPENAQ_API_KEY"}
        self.assertEqual(self.run_it(net, env=env), 0)
        self.assertFalse(any("openaq" in u for u in net.urls))
        self.assertEqual(sum("OPENAQ_API_KEY is not set" in line for line in self.lines), 1)
        self.assertNotIn("sensors", self.load("v2"))
        self.assert_bias_unchanged()

    def test_an_invalid_sensors_block_drops_only_the_block(self):
        old = output_v2.SENSOR_KEYS
        output_v2.SENSOR_KEYS = ["lat"]
        self.addCleanup(setattr, output_v2, "SENSOR_KEYS", old)
        self.assertEqual(self.run_it(self.good_net()), 0)
        v2 = self.load("v2")
        self.assertEqual(v2["version"], 2)
        self.assertIn("cells", v2)
        self.assertNotIn("sensors", v2)
        self.assertFalse(any("v2 failed" in line for line in self.lines))

    def test_v2_failure_still_publishes_the_bias_history(self):
        def broken(*a, **k):
            raise openaq.PipelineError("GFS down")
        run_job(ENV, out=self.paths["v1"], out_v2=self.paths["v2"], out_bias=self.paths["bias"],
                cache_dir=os.path.join(self.root, "cache"), gfs_cache_dir=os.path.join(
                    self.root, "gfs"), now_fn=lambda: NOW, log=self.lines.append,
                retrieve=fake_retrieve, fetch_wind=fake_wind, fetch_bundle=broken,
                http=http_of(self.good_net(published=self.HISTORY)))
        self.assert_bias_unchanged()

    def test_no_qualifying_sensor_omits_the_block_and_adds_no_rows(self):
        net = FakeOpenAQ([location(2, model="I-9PSL-DE")], {2: GOOD})
        self.assertEqual(self.run_it(net), 0)
        self.assertNotIn("sensors", self.load("v2"))
        self.assertEqual(self.bias_rows(), [])

    def test_the_key_is_sent_as_a_header_and_never_logged(self):
        net = self.good_net()
        self.run_it(net)
        self.assertTrue(net.keys and all(k == KEY for k in net.keys))
        self.assertFalse(any(KEY in url for url in net.urls))
        self.assertFalse(any(KEY in line for line in self.lines))

    def test_two_runs_do_not_duplicate_the_row(self):
        self.run_it(self.good_net())
        with open(self.paths["bias"]) as fh:
            first = fh.read()
        self.run_it(self.good_net(published=first))
        self.assertEqual(len(self.bias_rows()), 1)

    def test_a_blip_on_the_published_copy_falls_back_to_the_cache(self):
        self.run_it(self.good_net())
        os.remove(self.paths["bias"])
        self.assertEqual(self.run_it(self.good_net(published="down")), 0)
        self.assertEqual(len(self.bias_rows()), 1)

    def test_a_blip_with_no_cache_writes_no_bias_file_but_keeps_v2(self):
        self.assertEqual(self.run_it(self.good_net(published="down")), 0)
        self.assertFalse(os.path.exists(self.paths["bias"]))
        self.assertIn("sensors", self.load("v2"))
        self.assertTrue(any(line.startswith("ERROR") for line in self.lines))


if __name__ == "__main__":
    unittest.main()
