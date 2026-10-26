"""Writes reference back-paths for the app's Kotlin port from a real wind.json.

Usage: python scripts/wind_path_fixture.py WIND_JSON OUT_JSON
Decodes WIND_JSON with hazemy.windfile.decode and runs the Python back-trajectory with a point
every hour (49 points, rounded to 4 decimals) for five cases: three towns at the "now" of the
file, one 30 h in the past and one 33 h ahead. The port must reproduce these points.
"""
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hazemy import trajectory, windfile  # noqa: E402
from hazemy.common import fmt, parse_time  # noqa: E402

TOWNS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "towns.json")
CASES = (("Kuala Lumpur", 0), ("Kuching", 0), ("Kota Kinabalu", 0),
         ("Kuala Lumpur", -30), ("Johor Bahru", 33))


def build(doc: dict, towns: list) -> dict:
    by_name = {t["name"]: t for t in towns}
    fields, times = windfile.decode(doc)
    start = parse_time(doc["start_utc"])
    cases = []
    for name, offset_h in CASES:
        town = by_name[name]
        arrive = start + timedelta(hours=offset_h)
        (path,) = trajectory.back_trajectories(fields, times, [town["lat"]], [town["lon"]],
                                               arrive, every_h=1)
        cases.append({"town": name, "lat": town["lat"], "lon": town["lon"],
                      "arrive_utc": fmt(arrive),
                      "points": [[round(a, 4), round(b, 4)] for a, b in path]})
    return {"source": doc["generated_utc"], "cases": cases}


def main(wind_path: str, out_path: str) -> int:
    with open(wind_path) as fh:
        doc = json.load(fh)
    with open(TOWNS_PATH) as fh:
        towns = json.load(fh)["towns"]
    result = build(doc, towns)
    with open(out_path, "w") as fh:
        json.dump(result, fh, separators=(",", ":"))
    print(f"wrote {out_path} ({len(result['cases'])} cases)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
