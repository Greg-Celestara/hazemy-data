"""Sanity checks of path48h in a real v2 file, using the cached GFS slices (no download).

Usage: python scripts/check_trajectories.py site/v2/hazemy.json cache-gfs
For every cell the first segment (3 h) is compared with the 850 hPa wind over that segment, the
vector mean of the start slice and the slice 3 h earlier: it must head into that wind (within
45 deg) and be about speed x 3 h long (within 50%). Cells whose mean wind is below 2 m/s are
skipped, because a wind that weak and turning within 3 h has no meaningful direction. The path
must also have no jump over 250 km between points.
Prints the failures and the paths of Kuala Lumpur and Singapore (central).
"""
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hazemy import drivers, grid, trajectory  # noqa: E402
from hazemy.common import Budget, parse_time  # noqa: E402

CALM_MS = 2.0
MAX_BEARING_ERROR = 45.0
LENGTH_TOLERANCE = 0.5
MAX_JUMP_KM = 250.0  # a 3 h step at 20 m/s is 216 km


def refuse(*args, **kwargs):
    raise SystemExit("A GFS slice is missing from the cache; run the job first.")


def nearest(cells, lat, lon):
    return min(cells, key=lambda c: math.hypot(
        grid.cell_center(c["i"], c["j"])[0] - lat, grid.cell_center(c["i"], c["j"])[1] - lon))


def main(v2_path: str, cache: str) -> int:
    with open(v2_path) as fh:
        doc = json.load(fh)
    newest = parse_time(doc["sources"]["gfs_run_utc"])
    times = trajectory.slice_times(trajectory.trajectory_cycles(newest))
    fields = drivers.load_wind850(times, newest, cache, Budget(), refuse)
    start = times[-1]
    cells = doc["cells"]
    lats = [grid.cell_center(c["i"], c["j"])[0] for c in cells]
    lons = [grid.cell_center(c["i"], c["j"])[1] for c in cells]
    first = drivers.sample_wind(fields, start, np.array(lats), np.array(lons))
    before = drivers.sample_wind(fields, times[-2], np.array(lats), np.array(lons))
    us, vs = (first[0] + before[0]) / 2, (first[1] + before[1]) / 2
    bad = {"direction": 0, "length": 0, "jump": 0}
    checked = 0
    for k, cell in enumerate(cells):
        path = cell["path48h"]
        speed = math.hypot(us[k], vs[k])
        from_deg = math.degrees(math.atan2(-us[k], -vs[k])) % 360
        steps = [trajectory.distance_km(a, b) for a, b in zip(path, path[1:])]
        if speed >= CALM_MS and len(path) > 1:
            checked += 1
            err = abs((trajectory.bearing_deg(path[0], path[1]) - from_deg + 180) % 360 - 180)
            bad["direction"] += err > MAX_BEARING_ERROR
            want = speed * 3.6 * 3
            bad["length"] += abs(steps[0] - want) > LENGTH_TOLERANCE * want
        bad["jump"] += any(s > MAX_JUMP_KM for s in steps)
    print(f"{len(cells)} cells, {checked} with wind >= {CALM_MS} m/s checked over the 3 h "
          f"before {start:%Y-%m-%d %H}Z; failures: {bad}")
    places = (("Kuala Lumpur", (3.2, 101.6)), ("Singapore central", (1.36, 103.82)))
    for label, (lat, lon) in places:
        cell = nearest(cells, lat, lon)
        print(f"{label} (cell {cell['i']},{cell['j']}): {json.dumps(cell['path48h'])}")
    return 1 if any(bad.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
