"""The v2 job with GFS weather, MetMalaysia forecast and warnings added (D1-D3)."""
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from hazemy import met
from hazemy.common import PipelineError
from hazemy.job import run_job
from tests.test_data_sources import HEADER
from tests.test_job import ENV, FakeResponse, fake_retrieve, fake_wind
from tests.test_met import fixture_text
from tests.test_v2 import fake_bundle_factory, nea_body
from tests.test_weather import wx_bundle

NOW = datetime(2026, 10, 23, 10, 29, tzinfo=timezone.utc)
PLACES = {"places": [{"id": "Tn003", "name": "Alor Star", "lat": 6.12, "lon": 100.37}]}


class WeatherJob(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        path = os.path.join(self.root, "places.json")
        with open(path, "w") as fh:
            json.dump(PLACES, fh)
        patch = mock.patch.object(met, "PLACES_PATH", path)
        patch.start()
        self.addCleanup(patch.stop)
        self.lines, self.wx_calls = [], []

    def fetch_wx(self, cycle, fhr, kind, budget, log=print):
        self.wx_calls.append((cycle, fhr))
        return wx_bundle(fhr, temp_k=303.15, rh=70.0)

    def run_it(self, fetch_wx=None, forecast=True, warnings=True, now=NOW):
        def urlopen(req, timeout=None):
            url = req.full_url
            if "api.data.gov.my/weather/forecast" in url:
                if not forecast:
                    raise urllib.error.URLError("down")
                return FakeResponse(fixture_text("met_forecast.json"))
            if "api.data.gov.my/weather/warning" in url:
                if not warnings:
                    raise urllib.error.URLError("down")
                return FakeResponse(fixture_text("met_warnings.json"))
            if "data.gov.sg" in url:
                return FakeResponse(nea_body())
            return FakeResponse(HEADER)
        self.out = os.path.join(self.root, "v1", "hazemy.json")
        self.out2 = os.path.join(self.root, "v2", "hazemy.json")
        code = run_job(ENV, out=self.out, out_v2=self.out2,
                       cache_dir=os.path.join(self.root, "cache"),
                       gfs_cache_dir=os.path.join(self.root, "gfs"), now_fn=lambda: now,
                       log=self.lines.append, retrieve=fake_retrieve, fetch_wind=fake_wind,
                       fetch_bundle=fake_bundle_factory([]),
                       fetch_wx=fetch_wx or self.fetch_wx,
                       http={"urlopen": urlopen, "sleep": lambda s: None})
        self.assertEqual(code, 0)
        with open(self.out2) as fh:
            return json.load(fh)

    def test_every_cell_has_weather_now_today_and_each_day(self):
        doc = self.run_it()
        for cell in doc["cells"]:
            now = cell["now"]["weather"]
            self.assertEqual(now["temp_c"], 30.0)
            self.assertEqual(now["rh"], 70)
            self.assertIsInstance(now["feels_c"], int)
            self.assertEqual(now["rain_mm_3h"], 1.5)
            self.assertEqual(list(cell["today_weather"]),
                             ["temp_min_c", "temp_max_c", "feels_max_c", "rain_mm"])
            self.assertEqual(cell["today_weather"]["temp_max_c"], 30.0)
            for day in cell["days"]:
                self.assertEqual(day["weather"]["rain_mm"], 12.0)
                self.assertEqual(day["weather"]["temp_min_c"], 30.0)
        self.assertEqual(len(self.wx_calls), len(set(self.wx_calls)))

    def test_old_fields_keep_their_place_and_v1_has_no_weather(self):
        doc = self.run_it()
        cell = doc["cells"][0]
        self.assertEqual(list(cell["now"]), ["pm25_24h", "api", "band", "weather"])
        self.assertEqual(list(cell["days"][0])[:6],
                         ["date", "api", "api_min", "api_max", "band", "wind850"])
        self.assertEqual(list(cell)[:5], ["i", "j", "now", "tomorrow", "days"])
        with open(self.out) as fh:
            v1 = json.load(fh)
        self.assertNotIn("weather", v1["cells"][0]["now"])
        self.assertNotIn("warnings", v1)

    def test_met_and_warnings_are_added(self):
        doc = self.run_it()
        town = next(t for t in doc["towns"] if t["name"] == "Alor Setar")
        self.assertEqual((town["met"]["id"], town["met"]["place"]), ("Tn003", "Alor Star"))
        self.assertEqual(len(town["met"]["days"]), 4)
        self.assertTrue(all(t["country"] == "MY" for t in doc["towns"] if "met" in t))
        self.assertEqual(doc["sources"]["met_updated_utc"], "2026-10-23T10:29:00Z")
        self.assertEqual(len(doc["warnings"]), 3)
        self.assertEqual(list(doc)[-2:], ["towns", "warnings"])

    def test_gfs_weather_failure_gives_null_weather_and_everything_else_stays(self):
        def broken(cycle, fhr, kind, budget, log=print):
            raise PipelineError("NOMADS down")
        doc = self.run_it(fetch_wx=broken)
        cell = doc["cells"][0]
        self.assertEqual(set(cell["now"]["weather"].values()), {None})
        self.assertEqual(set(cell["today_weather"].values()), {None})
        self.assertEqual(set(cell["days"][0]["weather"].values()), {None})
        self.assertIn("rain_tonight_mm", cell["drivers"])
        self.assertTrue(any("GFS weather" in line for line in self.lines))

    def test_met_feeds_down_leaves_no_met_and_no_warnings(self):
        doc = self.run_it(forecast=False, warnings=False)
        self.assertFalse(any("met" in t for t in doc["towns"]))
        self.assertNotIn("met_updated_utc", doc["sources"])
        self.assertEqual(doc["warnings"], [])
        self.assertTrue(doc["cells"][0]["now"]["weather"]["temp_c"] is not None)


if __name__ == "__main__":
    unittest.main()
