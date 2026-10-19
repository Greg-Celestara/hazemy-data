"""The job: fetch, compute, assemble, validate, write. Exit code 0 only if the file was written.

Everything that can fail runs before write_atomic(), so a failure leaves the previous file alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from hazemy import cams, compute, firms, gfs
from hazemy import grid as gridmod
from hazemy import output
from hazemy.common import Budget, make_logger, require_env, utc_now

DEFAULT_OUT = os.path.join("site", "v1", "hazemy.json")
DEFAULT_CACHE = "cache"
TOWNS_PATH = os.path.join(gridmod.DATA_DIR, "towns.json")
PREV_RUN_LEAD_H = 24


def load_towns(path: str = TOWNS_PATH) -> list:
    with open(path) as fh:
        return json.load(fh)["towns"]


def cams_series(env, now, cells, cache_dir, budget, log, retrieve) -> tuple:
    """({valid: per-cell PM2.5}, newest run). Adds the previous run when the windows need it."""
    run = cams.expected_run(now)
    earliest = compute.earliest_needed(now)
    runs = [(run, compute.lead_hours(run, now))]
    if earliest < run:
        runs.insert(0, (run - timedelta(days=1), PREV_RUN_LEAD_H))
    lats, lons = gridmod.cell_arrays(cells)
    decoded = []
    for r, lead in runs:
        path = cams.cached_grib(r, lead, cache_dir, env["ADS_API_KEY"], budget,
                                retrieve=retrieve, log=log)
        decoded.append(cams.decode_grib(path))
    merged = cams.merge_runs(decoded)
    series = {t: cams.sample_cells(field, geometry, lats, lons)
              for t, (field, geometry) in merged.items()}
    return series, run


def run_job(env, *, out=DEFAULT_OUT, cache_dir=DEFAULT_CACHE, now_fn=utc_now, log=print,
            retrieve=cams.retrieve_grib, fetch_wind=gfs.fetch_wind_messages, http=None) -> int:
    """0 on success; 1 on any failure, with the published file untouched."""
    safe_log = make_logger([env.get("ADS_API_KEY"), env.get("FIRMS_MAP_KEY")], log)
    try:
        require_env(env, ["ADS_API_KEY", "FIRMS_MAP_KEY"])
        body = _build(env, now_fn, cache_dir, safe_log, retrieve, fetch_wind, http or {})
        output.write_atomic(out, body)
        safe_log(f"wrote {out} ({len(body)} bytes)")
        return 0
    except Exception as err:  # noqa: BLE001 - any failure must end as exit 1 with one clear line
        safe_log(f"ERROR: {type(err).__name__}: {err} The published file was not changed.")
        return 1


def _build(env, now_fn, cache_dir, log, retrieve, fetch_wind, http) -> bytes:
    now = now_fn()
    budget = Budget()
    provinces = gridmod.load_provinces()
    cells = gridmod.land_cells(gridmod.malaysia_land(provinces))
    log(f"{len(cells)} cells within {gridmod.LAND_RADIUS_KM:.0f} km of Malaysian land")
    series, cams_run = cams_series(env, now, cells, cache_dir, budget, log, retrieve)
    lats, lons = gridmod.cell_arrays(cells)
    today = compute.today_steps(now)
    wind_series, gfs_run = gfs.collect_wind(today, now, lats, lons, budget, log=log,
                                            fetch=fetch_wind)
    points = firms.fetch_detections(env["FIRMS_MAP_KEY"], now, budget, log=log, **http)
    fires = firms.count_by_province(points, provinces)
    doc = output.assemble(
        generated=now, cams_run=cams_run, firms_fetched=now_fn(),
        gfs_run=gfs_run, cells=cells, air=compute.cell_air(series, now),
        wind=compute.cell_wind(wind_series, today), fires=fires, towns=load_towns())
    log(f"downloaded {budget.total} bytes in total")
    return output.validate(doc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hazemy", description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT, help="output file (default %(default)s)")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="download cache directory")
    args = parser.parse_args(argv)
    return run_job(os.environ, out=args.out, cache_dir=args.cache)


if __name__ == "__main__":
    sys.exit(main())
