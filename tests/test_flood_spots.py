"""v2/flood-spots.json: the 10 DBKL hotspots, their fields, the KL box and distance to the road."""
import json
import pathlib
import tempfile
import unittest

from hazemy import flood_spots
from hazemy.common import PipelineError

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "flood_road_geometry.json"


def spots():
    return json.loads(flood_spots.SOURCE.read_text(encoding="utf-8"))


class FloodSpotsTest(unittest.TestCase):
    def test_build_writes_ten_spots_with_exact_fields_under_10_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "v2" / "flood-spots.json"
            size = flood_spots.build(str(out))
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 10)
        self.assertLess(size, 10_000)
        for spot in data:
            self.assertEqual(tuple(spot), flood_spots.FIELDS)
            self.assertEqual((spot["district"], spot["source"], spot["source_date"]),
                             ("Kuala Lumpur", "DBKL", "2026-05"))

    def test_every_spot_is_inside_the_kl_box(self):
        lat0, lon0, lat1, lon1 = flood_spots.KL_BBOX
        for s in spots():
            self.assertTrue(lat0 <= s["lat"] <= lat1 and lon0 <= s["lon"] <= lon1, s["id"])

    def test_every_pin_is_within_50_m_of_its_osm_road(self):
        ways = json.loads(FIXTURE.read_text())["ways"]
        for s in spots():
            d = flood_spots.point_to_ways_m(s["lat"], s["lon"], ways[s["id"]])
            print(f"{s['id']:36s} {s['lat']:.5f} {s['lon']:.5f} {d:6.1f} m")
            self.assertLessEqual(d, flood_spots.MAX_ROAD_DISTANCE_M, s["id"])

    def test_distance_function_known_value(self):
        # 0.001 degrees of latitude north of a west-east way is 111.32 m.
        d = flood_spots.point_to_ways_m(3.001, 101.0, [[[3.0, 100.9], [3.0, 101.1]]])
        self.assertAlmostEqual(d, 111.32, places=1)

    def test_validate_rejects_wrong_count_fields_and_location(self):
        good = spots()
        with self.assertRaises(PipelineError):
            flood_spots.validate(good[:9])
        extra = [dict(s, extra=1) for s in good]
        with self.assertRaises(PipelineError):
            flood_spots.validate(extra)
        moved = [dict(s) for s in good]
        moved[0]["lat"] = 5.4
        with self.assertRaises(PipelineError):
            flood_spots.validate(moved)

    def test_workflow_publishes_the_file(self):
        wf = (pathlib.Path(__file__).parent.parent / ".github/workflows/hazemy.yml").read_text()
        self.assertIn("hazemy.flood_spots", wf)


if __name__ == "__main__":
    unittest.main()
