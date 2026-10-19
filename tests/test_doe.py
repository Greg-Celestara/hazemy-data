"""DOE conversion, bands and compass, checked against docs/schema.md."""
import unittest

from hazemy.doe import band, compass, pm25_to_api


class PmToApi(unittest.TestCase):
    def test_breakpoint_edges(self):
        cases = {0: 0, 12.0: 50, 12.1: 51, 50.4: 100, 50.5: 101, 55.4: 150, 55.5: 151,
                 150.4: 200, 150.5: 201, 250.4: 300, 250.5: 301, 350.4: 400, 350.5: 401,
                 500.4: 500, 600: 500}
        for pm, expected in cases.items():
            with self.subTest(pm=pm):
                self.assertEqual(pm25_to_api(pm), expected)

    def test_concentration_rounds_to_one_decimal_before_lookup(self):
        self.assertEqual(pm25_to_api(12.04), 50)   # rounds to 12.0
        self.assertEqual(pm25_to_api(12.05), 51)   # rounds half up to 12.1

    def test_interpolates_linearly_inside_a_row(self):
        # Midway between 12.1 (51) and 50.4 (100): 31.25 -> 75.5 -> rounds half up to 76.
        self.assertEqual(pm25_to_api(31.3), 76)

    def test_nan_is_rejected(self):
        with self.assertRaises(ValueError):
            pm25_to_api(float("nan"))


class Bands(unittest.TestCase):
    def test_edges(self):
        cases = {0: 1, 50: 1, 51: 2, 100: 2, 101: 3, 200: 3, 201: 4, 300: 4, 301: 5, 500: 5}
        for api, expected in cases.items():
            with self.subTest(api=api):
                self.assertEqual(band(api), expected)


class Compass(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(compass(22.4), "N")
        self.assertEqual(compass(22.5), "NE")
        self.assertEqual(compass(337.5), "N")
        self.assertEqual(compass(337.4), "NW")

    def test_cardinal_points(self):
        for deg, name in ((0, "N"), (90, "E"), (180, "S"), (225, "SW"), (270, "W"), (360, "N")):
            self.assertEqual(compass(deg), name)


if __name__ == "__main__":
    unittest.main()
