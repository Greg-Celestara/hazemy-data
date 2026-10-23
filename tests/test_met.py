"""MetMalaysia forecast per town and warnings, from copies of the real 23 Oct 2026 feeds."""
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from hazemy import met
from hazemy.common import Budget

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = datetime(2026, 10, 23, 10, 29, tzinfo=timezone.utc)  # 18:29 MYT, as when the feeds were saved
CLEAR_SLEEP = {"sleep": lambda s: None}


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class Reply:
    status = 200

    def __init__(self, text):
        self.body = text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def feeds(forecast=None, warnings=None, seen=None):
    """A urlopen serving the fixtures; None for a feed makes it unreachable."""
    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, req.get_header("User-agent")))
        text = forecast if "forecast" in req.full_url else warnings
        if text is None:
            raise urllib.error.URLError("unreachable")
        return Reply(text)
    return urlopen


def warning_rows():
    return json.loads(fixture_text("met_warnings.json"))


class Matching(unittest.TestCase):
    TOWN = {"name": "Somewhere", "lat": 6.0, "lon": 100.0, "country": "MY"}

    def place(self, place_id, north_km):
        return {"id": place_id, "name": place_id, "lat": 6.0 + north_km / 111.195, "lon": 100.0}

    def test_the_cutoff_is_25_km(self):
        near, far = self.place("Tn001", 24.9), self.place("Tn002", 25.2)
        match = met.nearest_place(self.TOWN, [near])
        self.assertEqual(match[0]["id"], "Tn001")
        self.assertAlmostEqual(match[1], 24.9, places=1)
        self.assertIsNone(met.nearest_place(self.TOWN, [far]))

    def test_the_nearest_of_town_and_district_wins(self):
        places = [self.place("Ds001", 10.0), self.place("Tn001", 4.0), self.place("Ds002", 6.0)]
        self.assertEqual(met.nearest_place(self.TOWN, places)[0]["id"], "Tn001")

    def test_recreation_division_and_state_places_are_ignored(self):
        entries = [{"id": "Rc001", "name": "a", "lat": 6.0, "lon": 100.0},
                   {"id": "Dv501", "name": "b", "lat": 6.0, "lon": 100.0},
                   {"id": "St001", "name": "c", "lat": 6.0, "lon": 100.0},
                   {"id": "Ds009", "name": "d", "lat": 6.1, "lon": 100.0},
                   {"id": "Tn009", "name": "e", "lat": 6.2, "lon": 100.0}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            with open(path, "w") as fh:
                json.dump({"places": entries}, fh)
            places = met.load_places(path)
        self.assertEqual([p["id"] for p in places], ["Ds009", "Tn009"])
        self.assertEqual(met.nearest_place(self.TOWN, places)[0]["id"], "Ds009")

    def test_singapore_towns_get_no_match(self):
        sg = dict(self.TOWN, country="SG")
        matches = met.match_towns([self.TOWN, sg], [self.place("Tn001", 1.0)])
        self.assertIsNotNone(matches[0])
        self.assertIsNone(matches[1])

    def test_the_real_places_file_has_only_towns_and_districts_inside_malaysia(self):
        places = met.load_places()
        self.assertGreater(len(places), 100)
        for p in places:
            self.assertTrue(0.5 <= p["lat"] <= 7.6 and 99.4 <= p["lon"] <= 119.4, p)


class Forecast(unittest.TestCase):
    def rows(self):
        return json.loads(fixture_text("met_forecast.json"))

    def test_parse_keeps_only_wanted_places_and_the_feed_text_exactly(self):
        found = met.parse_forecast(self.rows(), {"Ds001"})
        self.assertEqual(list(found), ["Ds001"])
        first = found["Ds001"]["days"][0]
        raw = next(r for r in self.rows()
                   if r["location"]["location_id"] == "Ds001" and r["date"] == first["date"])
        self.assertEqual(first, {"date": raw["date"], "morning": raw["morning_forecast"],
                                 "afternoon": raw["afternoon_forecast"],
                                 "night": raw["night_forecast"], "summary": raw["summary_forecast"],
                                 "when": raw["summary_when"], "min_c": raw["min_temp"],
                                 "max_c": raw["max_temp"]})
        dates = [d["date"] for d in found["Ds001"]["days"]]
        self.assertEqual(dates, sorted(dates))

    def test_town_gets_today_plus_three_days_only(self):
        saved = met.parse_forecast(self.rows(), {"Ds001"})
        place = {"id": "Ds001", "name": "Langkawi"}
        out = met.town_met((place, 3.24), saved, NOW)
        self.assertEqual(out["place"], "Langkawi")
        self.assertEqual((out["id"], out["km"]), ("Ds001", 3.2))
        self.assertEqual([d["date"] for d in out["days"]],
                         ["2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"])
        self.assertEqual(list(out["days"][0]), ["date", "morning", "afternoon", "night", "summary",
                                                "when", "min_c", "max_c"])

    def test_only_dates_the_feed_has_are_listed(self):
        saved = met.parse_forecast(self.rows(), {"Ds001"})
        late = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)  # feed ends 29 Oct
        out = met.town_met(({"id": "Ds001"}, 1.0), saved, late)
        self.assertEqual([d["date"] for d in out["days"]], ["2026-10-28", "2026-10-29"])

    def test_null_and_repeated_dates_are_dropped_keeping_the_first(self):
        rows = self.rows()
        first = next(r for r in rows if r["location"]["location_id"] == "Ds001"
                     and r["date"] == "2026-10-24")
        repeat = dict(first, summary_forecast="SECOND COPY")
        null_date = dict(first, date=None)
        saved = met.parse_forecast(rows + [repeat, null_date], {"Ds001"})
        out = met.town_met(({"id": "Ds001", "name": "x"}, 1.0), saved, NOW)
        dates = [d["date"] for d in out["days"]]
        self.assertEqual(dates, ["2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"])
        self.assertEqual(out["days"][1]["summary"], first["summary_forecast"])

    def test_no_match_or_no_feed_rows_gives_no_met(self):
        self.assertIsNone(met.town_met(None, {}, NOW))
        self.assertIsNone(met.town_met(({"id": "Tn999"}, 1.0), {}, NOW))


class Loading(unittest.TestCase):
    TOWNS = [{"name": "Alor Setar", "lat": 6.12, "lon": 100.37, "country": "MY"},
             {"name": "Tampines", "lat": 1.35, "lon": 103.94, "country": "SG"}]

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cache = os.path.join(self._dir.name, "cache")
        places = os.path.join(self._dir.name, "places.json")
        with open(places, "w") as fh:
            json.dump({"places": [{"id": "Tn003", "name": "Alor Star", "lat": 6.12, "lon": 100.37},
                                  {"id": "Ds001", "name": "Langkawi", "lat": 6.35, "lon": 99.8}]},
                      fh)
        patch = mock.patch.object(met, "PLACES_PATH", places)
        patch.start()
        self.addCleanup(patch.stop)
        self.lines = []

    def load(self, now=NOW, **feed):
        return met.load_met(self.TOWNS, now, Budget(), self.cache, self.lines.append,
                            urlopen=feeds(**feed), **CLEAR_SLEEP)

    def good(self):
        return dict(forecast=fixture_text("met_forecast.json"),
                    warnings=fixture_text("met_warnings.json"))

    def test_one_request_per_feed_with_trailing_slash_and_plain_agent(self):
        seen = []
        out = self.load(seen=seen, **self.good())
        self.assertEqual(seen, [("https://api.data.gov.my/weather/forecast/", "HazeMY-data"),
                                ("https://api.data.gov.my/weather/warning/", "HazeMY-data")])
        self.assertEqual(out["met"][0]["id"], "Tn003")
        self.assertIsNone(out["met"][1])
        self.assertEqual(out["updated"], NOW)

    def test_failed_fetch_reuses_the_last_good_copy_within_24_hours(self):
        self.load(**self.good())
        later = NOW + timedelta(hours=23)
        out = self.load(now=later, forecast=None, warnings=None)
        self.assertEqual(out["met"][0]["id"], "Tn003")
        self.assertEqual(out["updated"], NOW)
        self.assertEqual(out["met"][0]["days"][0]["date"], "2026-10-24")

    def test_failed_fetch_drops_met_after_24_hours(self):
        self.load(**self.good())
        out = self.load(now=NOW + timedelta(hours=25), forecast=None, warnings=None)
        self.assertEqual(out["met"], [None, None])
        self.assertIsNone(out["updated"])

    def test_zero_rows_count_as_a_failure(self):
        self.load(**self.good())
        out = self.load(now=NOW + timedelta(hours=2), forecast="[]", warnings="[]")
        self.assertEqual(out["updated"], NOW)
        self.assertTrue(any("0 rows" in line for line in self.lines))

    def test_garbage_and_no_saved_copy_is_not_fatal(self):
        out = self.load(forecast="<html>", warnings="not json")
        self.assertEqual(out, {"met": [None, None], "updated": None, "warnings": []})

    def test_warnings_keep_the_last_copy_but_drop_expired_ones(self):
        self.load(**self.good())
        out = self.load(now=NOW + timedelta(hours=1), forecast=None, warnings=None)
        self.assertEqual(len(out["warnings"]), 3)  # thunderstorm and the two marine rows
        out = self.load(now=NOW + timedelta(hours=4), forecast=None, warnings=None)
        self.assertEqual([w["valid_to"] for w in out["warnings"]], ["2026-10-27T00:00:00+08:00"])

    def test_a_corrupt_saved_file_is_ignored(self):
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, met.SAVED_NAME), "w") as fh:
            fh.write("{broken")
        out = self.load(forecast=None, warnings=None)
        self.assertEqual(out["met"], [None, None])


class Warnings(unittest.TestCase):
    def parsed(self):
        return met.parse_warnings(warning_rows())

    def test_no_advisory_is_dropped(self):
        warnings = self.parsed()
        self.assertEqual(len(warnings), 4)
        self.assertNotIn("No Advisory", [w["title_en"] for w in warnings])

    def test_marine_rows_are_marine_with_level_cat2(self):
        marine = [w for w in self.parsed() if w["title_en"].startswith("Strong Winds")]
        self.assertEqual(len(marine), 3)
        for w in marine:
            self.assertTrue(w["marine"])
            self.assertEqual((w["type"], w["level"]), ("strong_wind", "cat2"))

    def test_thunderstorm_row_and_its_states(self):
        w = next(w for w in self.parsed() if w["type"] == "thunderstorm")
        self.assertFalse(w["marine"])
        self.assertIsNone(w["level"])
        self.assertEqual(w["states"], ["Kedah", "Penang", "Perak", "Kelantan", "Terengganu",
                                       "Pahang", "Negeri Sembilan", "Johor", "Sarawak", "Sabah"])

    def test_times_get_the_malaysian_offset_and_the_id_is_stable(self):
        w = next(w for w in self.parsed() if w["type"] == "thunderstorm")
        self.assertEqual((w["issued"], w["valid_from"], w["valid_to"]),
                         ("2026-10-23T17:55:00+08:00", "2026-10-23T17:00:00+08:00",
                          "2026-10-23T21:00:00+08:00"))
        self.assertRegex(w["id"], r"^[0-9a-f]{12}$")
        again = next(x for x in self.parsed() if x["type"] == "thunderstorm")
        self.assertEqual(w["id"], again["id"])
        self.assertEqual(len({x["id"] for x in self.parsed()}), 4)
        self.assertEqual(list(w), ["id", "type", "level", "marine", "issued", "valid_from",
                                   "valid_to", "title_en", "title_bm", "text_en", "text_bm",
                                   "instruction_en", "instruction_bm", "states"])

    def test_expired_warnings_are_dropped_and_the_edge_is_exclusive(self):
        warnings = self.parsed()
        self.assertEqual(len(met.unexpired(warnings, NOW)), 3)  # the 00:00 23 Oct row has expired
        evening = datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc)  # 21:00 MYT exactly
        self.assertEqual(len(met.unexpired(warnings, evening)), 1)
        after = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)  # 27 Oct 00:00 MYT
        self.assertEqual(met.unexpired(warnings, after), [])

    def rain_row(self, title_en, heading_en, heading_bm, title_bm):
        row = warning_rows()[3]
        row["warning_issue"] = dict(row["warning_issue"], title_en=title_en, title_bm=title_bm)
        row["heading_en"], row["heading_bm"] = heading_en, heading_bm
        return row

    def test_continuous_rain_is_heavy_rain_at_alert_level(self):
        row = self.rain_row("Continuous Rain Warning", "ALERT LEVEL CONTINUOUS RAIN WARNING",
                            "AMARAN HUJAN BERTERUSAN TAHAP WASPADA", "Amaran Hujan Berterusan")
        out = met.parse_warning(row)
        self.assertEqual((out["type"], out["level"]), ("heavy_rain", "alert"))

    def test_continuous_rain_at_danger_level(self):
        row = self.rain_row("Continuous Rain Warning", "DANGER LEVEL CONTINUOUS RAIN WARNING",
                            "AMARAN HUJAN BERTERUSAN TAHAP BAHAYA", "Amaran Hujan Berterusan")
        out = met.parse_warning(row)
        self.assertEqual((out["type"], out["level"]), ("heavy_rain", "danger"))

    def test_severe_continuous_rain_in_malay_only(self):
        row = self.rain_row("Rain", "WARNING", "AMARAN HUJAN BERTERUSAN TAHAP BURUK",
                            "Amaran Hujan Berterusan")
        out = met.parse_warning(row)
        self.assertEqual((out["type"], out["level"]), ("heavy_rain", "severe"))

    def test_a_timestamp_with_an_offset_keeps_it(self):
        row = warning_rows()[3]
        row["valid_to"] = "2026-10-27T00:00:00+08:00"
        row["valid_from"] = "2026-10-24T00:00:00Z"
        out = met.parse_warning(row)
        self.assertEqual(out["valid_to"], "2026-10-27T00:00:00+08:00")
        self.assertEqual(out["valid_from"], "2026-10-24T00:00:00+00:00")

    def test_one_unparseable_timestamp_drops_only_that_warning_and_logs_it(self):
        rows = warning_rows()
        rows[3]["valid_to"] = "next Tuesday"
        logged = []
        out = met.parse_warnings(rows, log=logged.append)
        self.assertEqual(len(out), len(met.parse_warnings(warning_rows())) - 1)
        self.assertEqual(len(logged), 1)
        self.assertIn("next Tuesday", logged[0])

    def test_null_validity_is_dropped(self):
        row = warning_rows()[3]
        row["valid_to"] = None
        self.assertIsNone(met.parse_warning(row))

    def test_state_aliases(self):
        text = "Areas: Pulau Pinang • N. Sembilan • W.P. Kuala Lumpur • Putrajaya • W.P. Labuan"
        text = text.replace(" • ", " | ")
        self.assertEqual(met.warning_states(text, ""),
                         ["Penang", "Negeri Sembilan", "Kuala Lumpur", "Putrajaya", "Labuan"])
        self.assertEqual(met.warning_states("", "negeri Pulau Pinang dan Perlis"),
                         ["Penang", "Perlis"])
        self.assertEqual(met.warning_states("Bandar Baharu only", ""), [])

    def test_english_states_come_before_malay_only_ones(self):
        self.assertEqual(met.warning_states("Perlis", "Kedah dan Perlis"), ["Perlis", "Kedah"])

    def test_type_keywords(self):
        for title, expected in (("Thunderstorms Warning", "thunderstorm"),
                                ("Heavy Rain Warning", "heavy_rain"),
                                ("Amaran Hujan Lebat", "heavy_rain"),
                                ("Strong Winds and Rough Seas Warning", "strong_wind"),
                                ("Hot Weather Warning", "heat"),
                                ("Tropical Cyclone Warning", "cyclone"),
                                ("Earthquake", "other")):
            self.assertEqual(met.warning_type(title, ""), expected, title)

    def test_levels(self):
        def row(title, heading, bm=""):
            return {"warning_issue": {"title_en": title, "title_bm": bm}, "heading_en": heading,
                    "text_en": "This is dangerous to small boats"}
        self.assertEqual(met.warning_level(row("Heavy Rain Warning", "ALERT LEVEL"), "heavy_rain"),
                         "alert")
        self.assertEqual(met.warning_level(row("Heavy Rain", "SEVERE LEVEL"), "heavy_rain"),
                         "severe")
        self.assertEqual(met.warning_level(row("Heavy Rain", "", "Amaran Tahap Bahaya"),
                                           "heavy_rain"), "danger")
        self.assertEqual(met.warning_level(row("Hot Weather Warning", "LEVEL 2"), "heat"), "heat2")
        self.assertEqual(met.warning_level(row("Strong Winds", "THIRD CATEGORY WARNING"),
                                           "strong_wind"), "cat3")
        self.assertIsNone(met.warning_level(row("Thunderstorms Warning", "THUNDERSTORMS"),
                                            "thunderstorm"))

    def test_marine_by_text(self):
        row = warning_rows()[3]
        self.assertFalse(met.is_marine(row))
        row["text_en"] = "Strong winds expected over the waters of Kedah"
        self.assertTrue(met.is_marine(row))


if __name__ == "__main__":
    unittest.main()
