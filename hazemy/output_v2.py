"""Assemble and validate hazemy.json v2 (docs/schema-v2.md)."""
from __future__ import annotations

import json

from hazemy import output
from hazemy.common import fmt
from hazemy.grid import GRID
from hazemy.trajectory import BOX, POINTS

TOP_KEYS = ["version", "generated_utc", "sources", "grid", "cells", "fires_72h", "fires_daily",
            "fire_bins", "towns"]
MAX_BINS = 1500
TRENDS = {"rising", "falling", "steady"}
V1_SOURCE_KEYS = {"cams_run_utc", "firms_fetched_utc", "firms_updated_utc", "gfs_run_utc",
                  "attribution"}


def assemble(*, v1_parts: dict, cells: list, air: list, wind: list, sg: list, drivers: list,
             paths: list, fires: list, fires_daily: list, bins: list, nea, towns: list,
             traj_cycles: list, nea_attribution: str) -> dict:
    """`v1_parts` are the arguments of output.assemble (generated, cams_run, ...)."""
    records = []
    for k, (i, j, _, _) in enumerate(cells):
        rec = {"i": i, "j": j, "now": air[k]["now"], "tomorrow": air[k]["tomorrow"],
               "wind_today": wind[k]}
        if sg[k] is not None:
            rec["sg"] = sg[k]
        rec["drivers"] = drivers[k]
        rec["path48h"] = paths[k]
        records.append(rec)
    base = output.assemble(cells=cells, air=air, wind=wind, fires=fires, towns=towns,
                           **v1_parts)
    sources = dict(base["sources"])
    sources["gfs_traj_cycles_utc"] = [fmt(c) for c in traj_cycles]
    doc = {"version": 2, "generated_utc": base["generated_utc"], "sources": sources,
           "grid": base["grid"], "cells": records, "fires_72h": fires,
           "fires_daily": fires_daily, "fire_bins": bins}
    if nea is not None:
        doc["nea"] = nea
        doc["sources"]["nea_updated_utc"] = nea["updated_utc"]
        doc["sources"]["nea_attribution"] = nea_attribution
    doc["towns"] = towns
    return doc


def _check_path(path: list, where: str) -> None:
    west, east, south, north = BOX
    output._need(1 <= len(path) <= POINTS, f"{where} path length {len(path)}")
    for lat, lon in path:
        output._need(west <= lon <= east and south <= lat <= north, f"{where} path point off box")


def _check_drivers(d: dict, where: str) -> None:
    need = output._need
    need(isinstance(d["rain_tonight_mm"], float) and d["rain_tonight_mm"] >= 0, f"{where} rain")
    for key in ("wind850_today", "wind850_tomorrow"):
        w = d[key]
        need(output._is_int(w["from_deg"]) and 0 <= w["from_deg"] < 360, f"{where} {key} dir")
        need(output._is_int(w["speed_kmh"]) and w["speed_kmh"] >= 0, f"{where} {key} speed")
    count = d["path_fire_count_72h"]
    need(output._is_int(count) and count >= 0, f"{where} path_fire_count_72h")
    if count == 0:
        need(d["path_fire_province"] is None and d["path_fire_trend"] is None,
             f"{where} fire fields must be null when count is 0")
    else:
        need(d["path_fire_trend"] in TRENDS, f"{where} path_fire_trend")


def _check_cell(cell: dict) -> None:
    where = f"cell ({cell['i']}, {cell['j']})"
    output._check_cell({k: cell[k] for k in ("i", "j", "now", "tomorrow", "wind_today")})
    _check_drivers(cell["drivers"], where)
    _check_path(cell["path48h"], where)
    sg = cell.get("sg")
    if sg:
        need = output._need
        need(0 <= sg["tomorrow_psi_min"] <= sg["tomorrow_psi"] <= sg["tomorrow_psi_max"] <= 500,
             f"{where} sg psi order")
        need(sg["band"] in range(1, 6), f"{where} sg band")


def _check_nea(nea: dict) -> None:
    output._need(set(nea["regions"]) == {"north", "south", "east", "west", "central"},
                 "nea regions")
    for region in nea["regions"].values():
        output._need(region["band"] in range(1, 6) and region["psi_24h"] >= 0, "nea region")


def validate(doc: dict) -> bytes:
    """Check shape and ranges, return compact JSON bytes under output.MAX_BYTES."""
    need = output._need
    expected = list(TOP_KEYS)
    if "nea" in doc:
        expected.insert(expected.index("towns"), "nea")
    need(list(doc) == expected, f"top-level keys {list(doc)}")
    need(doc["version"] == 2 and doc["grid"] == GRID, "version or grid")
    need(V1_SOURCE_KEYS <= set(doc["sources"]) and ("nea" in doc) == (
        "nea_updated_utc" in doc["sources"] and "nea_attribution" in doc["sources"]),
        "sources keys")
    need(len(doc["cells"]) > 0, "no cells")
    seen = set()
    for cell in doc["cells"]:
        _check_cell(cell)
        need((cell["i"], cell["j"]) not in seen, f"duplicate cell {cell['i']},{cell['j']}")
        seen.add((cell["i"], cell["j"]))
    fires = doc["fires_72h"]
    need(len(fires) <= 5 and all(f["count"] > 0 for f in fires), "fires_72h")
    need([f["province"] for f in fires] == [f["province"] for f in doc["fires_daily"]],
         "fires_daily provinces differ from fires_72h")
    need(all(len(f["counts"]) == 3 for f in doc["fires_daily"]), "fires_daily counts")
    need(len(doc["fire_bins"]) <= MAX_BINS and all(b[2] > 0 for b in doc["fire_bins"]),
         "fire_bins")
    if "nea" in doc:
        _check_nea(doc["nea"])
    towns = doc["towns"]
    need(len(towns) > 0 and all(t["country"] in ("MY", "SG") for t in towns), "towns")
    need(all(("nea_region" in t) == (t["country"] == "SG") for t in towns), "town nea_region")
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
    need(len(body) < output.MAX_BYTES, f"{len(body)} bytes is not under {output.MAX_BYTES}")
    return body
