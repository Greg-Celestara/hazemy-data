"""The job: fetch, compute, assemble, validate, write. Exit code 0 only if the file was written.

Everything that can fail runs before write_atomic(), so a failure leaves the previous file alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from shapely.ops import unary_union

from hazemy import cams, compute, drivers, firms, gfs, gfs_extra, output, output_v2
from hazemy import grid as gridmod
from hazemy import singapore, trajectory
from hazemy.common import Budget, PipelineError, make_logger, require_env, utc_now

DEFAULT_OUT = os.path.join("site", "v1", "hazemy.json")
DEFAULT_OUT_V2 = os.path.join("site", "v2", "hazemy.json")
DEFAULT_CACHE = "cache"
DEFAULT_GFS_CACHE = "cache-gfs"
TOWNS_PATH = os.path.join(gridmod.DATA_DIR, "towns.json")
PREV_RUN_LEAD_H = 24
# One fixed span for the newest run, long enough for any hour of the day (63 h from 16:20 UTC),
# so the cache filename is the same for every run of a day and ADS is asked once per run.
NEWEST_RUN_LEAD_H = 72


def load_towns(path: str = TOWNS_PATH) -> list:
    with open(path) as fh:
        return json.load(fh)["towns"]


def cams_series(env, now, cells, cache_dir, budget, log, retrieve) -> tuple:
    """({valid: per-cell PM2.5}, newest run). Adds the previous run when the windows need it."""
    run = cams.expected_run(now)
    earliest = compute.earliest_needed(now)
    needed = compute.lead_hours(run, now)
    if needed > NEWEST_RUN_LEAD_H:
        raise PipelineError(f"CAMS windows need {needed} h of lead; the request covers "
                            f"{NEWEST_RUN_LEAD_H} h. Raise NEWEST_RUN_LEAD_H.")
    runs = [(run, NEWEST_RUN_LEAD_H)]
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


def run_job(env, *, out=DEFAULT_OUT, out_v2=None, cache_dir=DEFAULT_CACHE,
            gfs_cache_dir=DEFAULT_GFS_CACHE, now_fn=utc_now, log=print,
            retrieve=cams.retrieve_grib, fetch_wind=gfs.fetch_wind_messages,
            fetch_bundle=gfs_extra.fetch_bundle, http=None) -> int:
    """0 on success; 1 on any failure, with the published files untouched.

    v2 is built only when `out_v2` is given. Both files are written after both are built, so a
    failure in either leaves the previous pair in place.
    """
    safe_log = make_logger([env.get("ADS_API_KEY"), env.get("FIRMS_MAP_KEY")], log)
    try:
        require_env(env, ["ADS_API_KEY", "FIRMS_MAP_KEY"])
        ctx = {"env": env, "now_fn": now_fn, "cache_dir": cache_dir, "gfs_cache": gfs_cache_dir,
               "log": safe_log, "retrieve": retrieve, "fetch_wind": fetch_wind,
               "fetch_bundle": fetch_bundle, "http": http or {}, "v2": out_v2 is not None}
        body, body_v2 = _build(ctx)
        output.write_atomic(out, body)
        safe_log(f"wrote {out} ({len(body)} bytes)")
        if body_v2 is not None:
            output.write_atomic(out_v2, body_v2)
            safe_log(f"wrote {out_v2} ({len(body_v2)} bytes)")
        return 0
    except Exception as err:  # noqa: BLE001 - any failure must end as exit 1 with one clear line
        safe_log(f"ERROR: {type(err).__name__}: {err} The published files were not changed.")
        return 1


def _cells(provinces, with_v2: bool) -> tuple:
    """(cells to compute, indices of the v1 subset). v2 adds cells near Singapore."""
    my_land = gridmod.malaysia_land(provinces)
    v1_cells = gridmod.land_cells(my_land)
    if not with_v2:
        return v1_cells, list(range(len(v1_cells)))
    cells = gridmod.land_cells(unary_union([my_land, singapore.load_land_km()]))
    v1_keys = {(c[0], c[1]) for c in v1_cells}
    return cells, [k for k, c in enumerate(cells) if (c[0], c[1]) in v1_keys]


def _build(ctx) -> tuple:
    now = ctx["now_fn"]()
    log, env = ctx["log"], ctx["env"]
    budget = Budget()
    provinces = gridmod.load_provinces()
    cells, v1_idx = _cells(provinces, ctx["v2"])
    log(f"{len(cells)} cells ({len(v1_idx)} within {gridmod.LAND_RADIUS_KM:.0f} km of Malaysia)")
    series, cams_run = cams_series(env, now, cells, ctx["cache_dir"], budget, log,
                                   ctx["retrieve"])
    lats, lons = gridmod.cell_arrays(cells)
    today = compute.today_steps(now)
    wind_series, gfs_run = gfs.collect_wind(today, now, lats, lons, budget, log=log,
                                            fetch=ctx["fetch_wind"])
    rows = firms.fetch_detection_rows(env["FIRMS_MAP_KEY"], now, budget, log=log, **ctx["http"])
    fires = firms.count_by_province([(r[0], r[1]) for r in rows], provinces)
    air = compute.cell_air(series, now)
    wind = compute.cell_wind(wind_series, today)
    v1_parts = {"generated": now, "cams_run": cams_run, "firms_fetched": ctx["now_fn"](),
                "gfs_run": gfs_run}
    doc = output.assemble(
        cells=[cells[k] for k in v1_idx], air=[air[k] for k in v1_idx],
        wind=[wind[k] for k in v1_idx], fires=fires, towns=load_towns(), **v1_parts)
    body_v2 = None
    if ctx["v2"]:
        doc_v2 = _build_v2(ctx, now, budget, v1_parts | {"fires": fires}, (cells, air, wind),
                           (series, rows, provinces))
        body_v2 = output_v2.validate(doc_v2)
    log(f"downloaded {budget.total} bytes in total")
    return output.validate(doc), body_v2


def _build_v2(ctx, now, budget, parts, per_cell, inputs) -> dict:
    cells, air, wind = per_cell
    series, rows, provinces = inputs
    fires = parts.pop("fires")
    log, fetch = ctx["log"], ctx["fetch_bundle"]
    lats, lons = gridmod.cell_arrays(cells)
    newest = parts["gfs_run"]
    tcycles = trajectory.trajectory_cycles(newest)
    tomorrow = compute.myt_day_bounds(now, 1)
    today_steps = compute.today_steps(now)
    tomorrow_steps = compute.steps_between(*tomorrow)
    times = trajectory.slice_times(tcycles)
    fields = drivers.load_wind850(set(times) | set(today_steps) | set(tomorrow_steps), newest,
                                  ctx["gfs_cache"], budget, fetch, log)
    paths = trajectory.back_trajectories(fields, times, lats, lons)
    rain = drivers.rain_tonight(now, newest, lats, lons, ctx["gfs_cache"], budget, fetch, log)
    gfs_extra.prune_cache(ctx["gfs_cache"], newest)
    w_today = drivers.wind850_means(fields, today_steps, lats, lons)
    w_tomorrow = drivers.wind850_means(fields, tomorrow_steps, lats, lons)
    det = drivers.Detections(rows, firms.locate_provinces([(r[0], r[1]) for r in rows],
                                                          provinces), now)
    per_driver = [{"rain_tonight_mm": rain[k], "wind850_today": w_today[k],
                   "wind850_tomorrow": w_tomorrow[k], **drivers.path_fire_stats(paths[k], det)}
                  for k in range(len(cells))]
    sg_all = singapore.cell_sg(series, now)
    near_sg = singapore.sg_flags(cells, singapore.load_land_km())
    nea = singapore.fetch_nea(now, budget, log=log, **ctx["http"])
    return output_v2.assemble(
        v1_parts=parts, cells=cells, air=air, wind=wind,
        sg=[sg_all[k] if near_sg[k] else None for k in range(len(cells))],
        drivers=per_driver, paths=[trajectory.rounded(p) for p in paths], fires=fires,
        fires_daily=drivers.fires_daily(fires, det), bins=drivers.fire_bins(det), nea=nea,
        towns=load_towns_v2(), traj_cycles=tcycles,
        nea_attribution=singapore.attribution(now))


def load_towns_v2() -> list:
    my = [{**t, "country": "MY"} for t in load_towns()]
    return my + singapore.load_towns()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hazemy", description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT, help="output file (default %(default)s)")
    parser.add_argument("--out-v2", default=DEFAULT_OUT_V2, help="v2 file (default %(default)s)")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="CAMS download cache directory")
    parser.add_argument("--gfs-cache", default=DEFAULT_GFS_CACHE, help="GFS cache directory")
    args = parser.parse_args(argv)
    return run_job(os.environ, out=args.out, out_v2=args.out_v2, cache_dir=args.cache,
                   gfs_cache_dir=args.gfs_cache)


if __name__ == "__main__":
    sys.exit(main())
