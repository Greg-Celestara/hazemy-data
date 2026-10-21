"""The job: fetch, compute, assemble, validate, write. Exit code 0 only if v1 was written.

Everything that can fail in v1 runs before write_atomic(), so a failure leaves the previous v1
alone. v2 is built afterwards and can never stop v1.
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
from hazemy.common import (Budget, PipelineError, http_get, make_logger, require_env,
                           utc_now)

DEFAULT_OUT = os.path.join("site", "v1", "hazemy.json")
DEFAULT_OUT_V2 = os.path.join("site", "v2", "hazemy.json")
PREVIOUS_V2_URL = "https://greg-celestara.github.io/hazemy-data/v2/hazemy.json"
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
    """0 when v1 was written; 1 on any v1 failure, with the published files untouched.

    v1 never depends on v2: it is built and written first. v2 is built only when `out_v2` is
    given, in a guarded block: if it fails, the error is logged, the previously published v2 is
    downloaded into `out_v2` (nothing is written if that fails too) and the exit code is still 0
    so the deploy publishes v1.
    """
    safe_log = make_logger([env.get("ADS_API_KEY"), env.get("FIRMS_MAP_KEY")], log)
    memo = {}

    def wind_once(cycle, fhr, budget, log=print):
        if (cycle, fhr) not in memo:
            memo[(cycle, fhr)] = fetch_wind(cycle, fhr, budget, log=log)
        return memo[(cycle, fhr)]

    try:
        require_env(env, ["ADS_API_KEY", "FIRMS_MAP_KEY"])
        ctx = {"env": env, "now_fn": now_fn, "cache_dir": cache_dir, "gfs_cache": gfs_cache_dir,
               "log": safe_log, "retrieve": retrieve, "fetch_wind": wind_once,
               "fetch_bundle": fetch_bundle, "http": http or {}}
        budget = Budget()
        body, shared = _build_v1(ctx, budget)
        output.write_atomic(out, body)
        safe_log(f"wrote {out} ({len(body)} bytes)")
    except Exception as err:  # noqa: BLE001 - any failure must end as exit 1 with one clear line
        safe_log(f"ERROR: {type(err).__name__}: {err} The published files were not changed.")
        return 1
    if out_v2 is not None:
        _publish_v2(ctx, budget, shared, out_v2)
    return 0


def _publish_v2(ctx, budget, shared, out_v2) -> None:
    log = ctx["log"]
    try:
        body_v2 = _build_v2(ctx, budget, shared)
        output.write_atomic(out_v2, body_v2)
        log(f"wrote {out_v2} ({len(body_v2)} bytes)")
    except Exception as err:  # noqa: BLE001 - v2 must never stop v1 from being published
        log(f"ERROR: v2 failed, v1 is still published. {type(err).__name__}: {err}")
        _restore_previous_v2(ctx, budget, out_v2)


def _restore_previous_v2(ctx, budget, out_v2) -> None:
    """Put the currently published v2 into `out_v2`; publish no v2 if it cannot be fetched."""
    log = ctx["log"]
    try:
        raw = http_get(PREVIOUS_V2_URL, label="previous v2", budget=budget, log=log,
                       **ctx["http"])
        body = output_v2.validate(json.loads(raw))
        output.write_atomic(out_v2, body)
        log(f"restored the previously published v2 into {out_v2} ({len(body)} bytes)")
    except Exception as err:  # noqa: BLE001 - the fallback itself must not fail the job
        log(f"ERROR: previous v2 not restored, publishing no v2 file. "
            f"{type(err).__name__}: {err}")


def _cells(provinces, with_sg: bool) -> list:
    """Cells within reach of Malaysia, plus those near Singapore when `with_sg`."""
    my_land = gridmod.malaysia_land(provinces)
    if not with_sg:
        return gridmod.land_cells(my_land)
    return gridmod.land_cells(unary_union([my_land, singapore.load_land_km()]))


def _build_v1(ctx, budget) -> tuple:
    """(v1 body, inputs v2 reuses)."""
    now = ctx["now_fn"]()
    log, env = ctx["log"], ctx["env"]
    provinces = gridmod.load_provinces()
    cells = _cells(provinces, False)
    log(f"{len(cells)} cells within {gridmod.LAND_RADIUS_KM:.0f} km of Malaysia")
    air, wind, cams_run, gfs_run, _ = _cell_values(ctx, now, cells, budget)
    rows, firms_updated = firms.rows_with_fallback(
        env["FIRMS_MAP_KEY"], now, ctx["now_fn"], budget, ctx["cache_dir"], log=log,
        **ctx["http"])
    fires = firms.count_by_province([(r[0], r[1]) for r in rows], provinces)
    parts = {"generated": now, "cams_run": cams_run, "firms_fetched": ctx["now_fn"](),
             "firms_updated": firms_updated, "gfs_run": gfs_run}
    doc = output.assemble(cells=cells, air=air, wind=wind, fires=fires, towns=load_towns(),
                          **parts)
    body = output.validate(doc)
    log(f"downloaded {budget.total} bytes so far")
    return body, {"now": now, "parts": parts, "fires": fires, "rows": rows,
                  "provinces": provinces}


def _cell_values(ctx, now, cells, budget) -> tuple:
    """(air, wind, CAMS run, GFS cycle, CAMS series): the first two per cell."""
    series, cams_run = cams_series(ctx["env"], now, cells, ctx["cache_dir"], budget,
                                   ctx["log"], ctx["retrieve"])
    lats, lons = gridmod.cell_arrays(cells)
    today = compute.today_steps(now)
    wind_series, gfs_run = gfs.collect_wind(today, now, lats, lons, budget, log=ctx["log"],
                                            fetch=ctx["fetch_wind"])
    return compute.cell_air(series, now), compute.cell_wind(wind_series, today), cams_run, \
        gfs_run, series


def _build_v2(ctx, budget, shared) -> bytes:
    now, provinces, rows = shared["now"], shared["provinces"], shared["rows"]
    log, fetch = ctx["log"], ctx["fetch_bundle"]
    cells = _cells(provinces, True)
    log(f"v2: {len(cells)} cells")
    air, wind, cams_run, gfs_run, series = _cell_values(ctx, now, cells, budget)
    parts = dict(shared["parts"], cams_run=cams_run, gfs_run=gfs_run)
    lats, lons = gridmod.cell_arrays(cells)
    today_steps = compute.today_steps(now)
    tomorrow_steps = compute.steps_between(*compute.myt_day_bounds(now, 1))
    fields, times, tcycles, used = drivers.load_trajectory_wind(
        now, gfs_run, set(today_steps) | set(tomorrow_steps), ctx["gfs_cache"], budget, fetch,
        log)
    start = trajectory.start_time(now)
    log(f"v2: paths start {start:%Y-%m-%d %H} UTC, GFS cycle {used:%Y-%m-%d %H} UTC")
    paths = trajectory.back_trajectories(fields, times, lats, lons, start)
    rain = drivers.rain_tonight(now, used, lats, lons, ctx["gfs_cache"], budget, fetch, log)
    gfs_extra.prune_cache(ctx["gfs_cache"], used)
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
    fires = shared["fires"]
    doc = output_v2.assemble(
        v1_parts=parts, cells=cells, air=air, wind=wind,
        sg=[sg_all[k] if near_sg[k] else None for k in range(len(cells))],
        drivers=per_driver, paths=[trajectory.rounded(p) for p in paths], fires=fires,
        fires_daily=drivers.fires_daily(fires, det), bins=drivers.fire_bins(det), nea=nea,
        towns=load_towns_v2(), traj_cycles=tcycles, nea_attribution=singapore.attribution(now))
    body = output_v2.validate(doc)
    log(f"downloaded {budget.total} bytes in total")
    return body


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
