"""Three forecast days (tomorrow, day +2, day +3) in v2: MYT boundaries, CAMS and wind gaps."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy import cams, compute, job, output_v2, singapore
from hazemy.common import PipelineError
from tests.test_job import NOW, fake_retrieve
from tests import test_v2  # module import: a class import would re-run JobV2's tests here

UTC = timezone.utc


def ramp_series(now):
    """PM2.5 rising 1 ug/m3 per 3 h step across every time the three days read."""
    times = sorted(set(compute.required_times(now)).union(*(
        compute.day_times(now, o) for o in (1, 2, 3))))
    return {t: np.array([10.0 + k]) for k, t in enumerate(times)}


class ForecastDays(unittest.TestCase):
    def test_myt_day_boundaries_and_dates(self):
        # 10:30 UTC on 19 Oct is 18:30 MYT on 19 Oct; MYT days start at 16:00 UTC.
        first = [compute.day_steps(NOW, o)[0] for o in (1, 2, 3)]
        last = [compute.day_steps(NOW, o)[-1] for o in (1, 2, 3)]
        self.assertEqual(first, [datetime(2026, 10, d, 18, tzinfo=UTC) for d in (19, 20, 21)])
        self.assertEqual(last, [datetime(2026, 10, d, 15, tzinfo=UTC) for d in (20, 21, 22)])
        days = compute.cell_days(ramp_series(NOW), NOW, [1, 2, 3])[0]
        self.assertEqual([d["date"] for d in days], ["2026-10-20", "2026-10-21", "2026-10-22"])

    def test_dates_use_the_myt_day_after_16_utc(self):
        late = datetime(2026, 10, 19, 16, 20, tzinfo=UTC)  # already 20 Oct in MYT
        self.assertEqual([compute.day_date(late, o) for o in (1, 2, 3)],
                         ["2026-10-21", "2026-10-22", "2026-10-23"])

    def test_each_day_is_its_own_24h_mean(self):
        series = ramp_series(NOW)
        days = compute.cell_days(series, NOW, [1, 2, 3])[0]
        for offset, day in zip((1, 2, 3), days):
            mean = float(np.mean([series[t][0] for t in compute.day_steps(NOW, offset)]))
            self.assertEqual(day["api"], compute.pm25_to_api(mean))
        self.assertLess(days[0]["api"], days[1]["api"])
        self.assertLess(days[1]["api"], days[2]["api"])

    def test_first_day_equals_tomorrow(self):
        series = ramp_series(NOW)
        tomorrow = compute.cell_air(series, NOW)[0]["tomorrow"]
        first = compute.cell_days(series, NOW, [1, 2, 3])[0][0]
        self.assertEqual({k: first[k] for k in tomorrow}, tomorrow)

    def test_short_run_gives_fewer_offsets_without_padding(self):
        series = ramp_series(NOW)
        last_day2 = compute.day_steps(NOW, 2)[-1]
        short = {t: v for t, v in series.items() if t <= last_day2}
        self.assertEqual(compute.forecast_offsets(short, NOW), [1, 2])
        only_one = {t: v for t, v in series.items() if t <= compute.day_steps(NOW, 1)[-1]}
        self.assertEqual(compute.forecast_offsets(only_one, NOW), [1])
        self.assertEqual(compute.forecast_offsets(series, NOW), [1, 2, 3])

    def test_a_gap_in_day_two_drops_day_three_too(self):
        series = ramp_series(NOW)
        del series[compute.day_steps(NOW, 2)[3]]
        self.assertEqual(compute.forecast_offsets(series, NOW), [1])

    def test_requested_lead_covers_day_three_at_every_hour(self):
        for hour in range(24):
            now = datetime(2026, 10, 19, hour, 20, tzinfo=UTC)
            run = cams.expected_run(now)
            need = (compute.last_forecast_time(now) - run) / timedelta(hours=1)
            self.assertLessEqual(need, job.NEWEST_RUN_LEAD_H, hour)
            self.assertLessEqual(job.NEWEST_RUN_LEAD_H, 120)  # CAMS publishes to 120 h


class SingaporeDays(unittest.TestCase):
    def test_days_psi_use_the_nea_method_per_day(self):
        series = ramp_series(NOW)
        sg = singapore.cell_sg(series, NOW, [1, 2, 3])[0]
        self.assertEqual(len(sg["days_psi"]), 3)
        for offset, entry in zip((1, 2, 3), sg["days_psi"]):
            mean = float(np.mean([series[t][0] for t in compute.day_steps(NOW, offset)]))
            self.assertEqual(entry["psi"], singapore.pm25_to_psi(mean))
            self.assertLessEqual(entry["psi_min"], entry["psi"])
            self.assertLessEqual(entry["psi"], entry["psi_max"])
        first = sg["days_psi"][0]
        self.assertEqual((first["psi"], first["psi_min"], first["psi_max"], first["band"]),
                         (sg["tomorrow_psi"], sg["tomorrow_psi_min"], sg["tomorrow_psi_max"],
                          sg["band"]))

    def test_days_psi_follow_the_offsets_given(self):
        sg = singapore.cell_sg(ramp_series(NOW), NOW, [1, 2])[0]
        self.assertEqual(len(sg["days_psi"]), 2)


class JobDays(unittest.TestCase):
    """Drives the two-file job through the JobV2 harness without re-running its tests."""

    def setUp(self):
        self.case = test_v2.JobV2("run_it")
        self.case.setUp()
        self.addCleanup(self.case._dir.cleanup)
        self.calls = self.case.calls

    def run_it(self, **kwargs):
        return self.case.run_it(**kwargs)

    def doc(self):
        return self.case.read(self.case.out2)

    def test_v2_has_three_days_matching_tomorrow(self):
        self.assertEqual(self.run_it(nea=test_v2.nea_body()), 0)
        cells = self.doc()["cells"]
        kl = next(c for c in cells if (c["i"], c["j"]) == (6, 5))
        self.assertEqual(len(kl["days"]), 3)
        self.assertEqual([d["date"] for d in kl["days"]],
                         ["2026-10-20", "2026-10-21", "2026-10-22"])
        for cell in cells:
            first = cell["days"][0]
            self.assertEqual({k: first[k] for k in cell["tomorrow"]}, cell["tomorrow"])
            self.assertEqual(first["wind850"], cell["drivers"]["wind850_tomorrow"])
            self.assertTrue(all(d["wind850"] is not None for d in cell["days"]))
        sg = next(c for c in cells if "sg" in c)["sg"]
        self.assertEqual([d["date"] for d in sg["days_psi"]],
                         [d["date"] for d in next(c for c in cells if "sg" in c)["days"]])

    def test_short_cams_run_gives_two_days(self):
        def short(run, lead, target, key):
            fake_retrieve(run, min(lead, 66), target, key)  # day +2 ends at 63 h, day +3 at 87 h
        self.assertEqual(self.run_it(nea=test_v2.nea_body(), retrieve=short), 0)
        cells = self.doc()["cells"]
        self.assertTrue(all(len(c["days"]) == 2 for c in cells))
        self.assertTrue(all(len(c["sg"]["days_psi"]) == 2 for c in cells if "sg" in c))

    def run_with_wind_limit(self, only_newest):
        inner = test_v2.fake_bundle_factory(self.calls)
        newest = datetime(2026, 10, 19, 0, tzinfo=UTC)

        def fetch(cycle, fhr, kind, budget, log=print):
            if kind == "w850" and fhr > 63 and (cycle == newest or not only_newest):
                raise PipelineError(f"GFS {cycle:%d %H} f{fhr} not on the bucket")
            return inner(cycle, fhr, kind, budget, log)
        self.assertEqual(self.run_it(nea=test_v2.nea_body(), bundle=fetch), 0)
        return self.doc()["cells"]

    def test_day_without_wind_gets_null_and_the_others_keep_theirs(self):
        for cell in self.run_with_wind_limit(only_newest=False):
            winds = [d["wind850"] for d in cell["days"]]
            self.assertIsNone(winds[2])
            self.assertIsNotNone(winds[0])
            self.assertIsNotNone(winds[1])
            self.assertEqual(len(cell["days"]), 3)

    def test_previous_cycle_supplies_wind_when_the_newest_lacks_a_day(self):
        for cell in self.run_with_wind_limit(only_newest=True):
            self.assertTrue(all(d["wind850"] is not None for d in cell["days"]))


class ValidationDays(unittest.TestCase):
    def setUp(self):
        case = JobDays("run_it")
        case.setUp()
        self.addCleanup(case.case._dir.cleanup)
        case.run_it(nea=test_v2.nea_body())
        self.good = case.doc()

    def test_valid_document_passes(self):
        output_v2.validate(self.good)

    def test_first_day_must_equal_tomorrow(self):
        doc = copy.deepcopy(self.good)
        doc["cells"][0]["days"][0]["api"] += 1
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_dates_must_be_consecutive(self):
        doc = copy.deepcopy(self.good)
        doc["cells"][0]["days"][2]["date"] = "2026-10-30"
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)

    def test_more_than_three_days_is_rejected(self):
        doc = copy.deepcopy(self.good)
        days = doc["cells"][0]["days"]
        days.append(dict(days[-1], date="2026-10-23"))
        with self.assertRaises(PipelineError):
            output_v2.validate(doc)
