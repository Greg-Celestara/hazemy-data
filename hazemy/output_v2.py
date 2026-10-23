"""Assemble and validate hazemy.json v2 (docs/schema-v2.md)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from hazemy import output
from hazemy.common import fmt
from hazemy.grid import GRID
from hazemy.trajectory import BOX, POINTS

TOP_KEYS = ["version", "generated_utc", "sources", "grid", "cells", "fires_72h", "fires_daily",
            "fire_bins", "towns", "warnings"]
MAX_BINS = 1500
DAY_KEYS = ["date", "api", "api_min", "api_max", "band", "wind850", "weather"]
NOW_WEATHER_KEYS = ["temp_c", "rh", "feels_c", "rain_mm_3h"]
SPAN_WEATHER_KEYS = ["temp_min_c", "temp_max_c", "feels_max_c", "rain_mm"]
WARNING_KEYS = ["id", "type", "level", "marine", "issued", "valid_from", "valid_to", "title_en",
                "title_bm", "text_en", "text_bm", "instruction_en", "instruction_bm", "states"]
MET_DAY_KEYS = ["date", "morning", "afternoon", "night", "summary", "when", "min_c", "max_c"]
MET_MAX_KM = 25.0
# v1 stays under output.MAX_BYTES (500 KB); weather and Met add about 230 KB to v2.
MAX_BYTES = 700_000
TRENDS = {"rising", "falling", "steady"}
V1_SOURCE_KEYS = {"cams_run_utc", "firms_fetched_utc", "firms_updated_utc", "gfs_run_utc",
                  "attribution"}


def assemble(*, v1_parts: dict, cells: list, air: list, wind: list, sg: list, days: list,
             drivers: list, paths: list, fires: list, fires_daily: list, bins: list, nea,
             towns: list, traj_cycles: list, nea_attribution: str, weather: dict,
             met_updated=None, warnings: list = ()) -> dict:
    """`v1_parts` are the arguments of output.assemble (generated, cams_run, ...).

    `weather` is weather.cell_weather(): per cell now, today and per-day values; the new
    fields are added after the existing keys of each object.
    """
    records = []
    for k, (i, j, _, _) in enumerate(cells):
        rec = {"i": i, "j": j, "now": {**air[k]["now"], "weather": weather["now"][k]},
               "tomorrow": air[k]["tomorrow"], "days": days[k], "wind_today": wind[k]}
        if sg[k] is not None:
            rec["sg"] = sg[k]
        rec["drivers"] = drivers[k]
        rec["path48h"] = paths[k]
        rec["today_weather"] = weather["today"][k]
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
    if met_updated is not None:
        doc["sources"]["met_updated_utc"] = fmt(met_updated)
    doc["towns"] = towns
    doc["warnings"] = list(warnings)
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


def _check_wind(w, where: str) -> None:
    if w is None:
        return
    need = output._need
    need(output._is_int(w["from_deg"]) and 0 <= w["from_deg"] < 360, f"{where} wind dir")
    need(output._is_int(w["speed_kmh"]) and w["speed_kmh"] >= 0, f"{where} wind speed")


def _check_days(cell: dict, where: str) -> None:
    need = output._need
    days, tom = cell["days"], cell["tomorrow"]
    need(1 <= len(days) <= 3, f"{where} days length {len(days)}")
    dates = [date.fromisoformat(d["date"]) for d in days]
    need(all(b - a == timedelta(days=1) for a, b in zip(dates, dates[1:])), f"{where} day dates")
    for n, d in enumerate(days):
        need(list(d) == DAY_KEYS, f"{where} day keys")
        need(all(output._is_int(d[k]) and 0 <= d[k] <= 500 for k in ("api", "api_min", "api_max")),
             f"{where} day {n} api")
        need(d["api_min"] <= d["api_max"] and d["band"] in range(1, 6), f"{where} day {n} range")
        _check_wind(d["wind850"], f"{where} day {n}")
        _check_weather(d["weather"], SPAN_WEATHER_KEYS, f"{where} day {n}")
    first = {k: days[0][k] for k in tom}
    need(first == tom, f"{where} days[0] differs from tomorrow")
    need(days[0]["wind850"] == cell["drivers"]["wind850_tomorrow"], f"{where} days[0] wind")
    psi = (cell.get("sg") or {}).get("days_psi")
    if psi is not None:
        need([d["date"] for d in psi] == [d["date"] for d in days][:len(psi)], f"{where} psi dates")
        need(all(0 <= d["psi_min"] <= d["psi"] <= d["psi_max"] <= 500 and d["band"] in range(1, 6)
                 for d in psi), f"{where} days_psi")


def _check_weather(w: dict, keys: list, where: str) -> None:
    """Each field is a number of the right kind and range, or null."""
    need = output._need
    need(isinstance(w, dict) and list(w) == keys, f"{where} weather keys")
    for key, value in w.items():
        if value is None:
            continue
        if key == "rh":
            ok = output._is_int(value) and 0 <= value <= 100
        elif key in ("feels_c", "feels_max_c"):
            ok = output._is_int(value) and -90 <= value <= 90
        elif key.startswith("rain"):
            ok = isinstance(value, float) and value >= 0
        else:
            ok = isinstance(value, float) and -90 <= value <= 90
        need(ok, f"{where} {key}={value!r}")


def _check_cell(cell: dict) -> None:
    where = f"cell ({cell['i']}, {cell['j']})"
    now = {k: v for k, v in cell["now"].items() if k != "weather"}
    output._check_cell({"i": cell["i"], "j": cell["j"], "now": now, "tomorrow": cell["tomorrow"],
                        "wind_today": cell["wind_today"]})
    _check_weather(cell["now"]["weather"], NOW_WEATHER_KEYS, f"{where} now")
    _check_weather(cell["today_weather"], SPAN_WEATHER_KEYS, f"{where} today")
    _check_drivers(cell["drivers"], where)
    _check_path(cell["path48h"], where)
    _check_days(cell, where)
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


def _check_met(town: dict) -> None:
    need = output._need
    met = town.get("met")
    if met is None:
        return
    where = f"town {town['name']} met"
    need(town["country"] == "MY" and met["id"][:2] in ("Tn", "Ds"), f"{where} place")
    need(0 <= met["km"] <= MET_MAX_KM, f"{where} km")
    dates = [d["date"] for d in met["days"]]
    need(1 <= len(dates) <= 4 and dates == sorted(set(dates)), f"{where} dates")
    need(all(list(d) == MET_DAY_KEYS for d in met["days"]), f"{where} day keys")


def _check_warning(w: dict) -> None:
    need = output._need
    need(list(w) == WARNING_KEYS, f"warning keys {list(w)}")
    need(w["type"] in ("thunderstorm", "heavy_rain", "strong_wind", "heat", "cyclone", "other"),
         "warning type")
    need(isinstance(w["marine"], bool) and isinstance(w["states"], list), "warning marine/states")
    start, end = (datetime.fromisoformat(w[k]) for k in ("valid_from", "valid_to"))
    need(start.utcoffset() is not None and end > start - timedelta(days=1), "warning times")


def validate(doc: dict) -> bytes:
    """Check shape and ranges, return compact JSON bytes under MAX_BYTES."""
    need = output._need
    expected = list(TOP_KEYS)
    if "nea" in doc:
        expected.insert(expected.index("towns"), "nea")
    need(list(doc) == expected, f"top-level keys {list(doc)}")
    need(doc["version"] == 2 and doc["grid"] == GRID, "version or grid")
    need(V1_SOURCE_KEYS <= set(doc["sources"]) and ("nea" in doc) == (
        "nea_updated_utc" in doc["sources"] and "nea_attribution" in doc["sources"]),
        "sources keys")
    output.check_firms_updated(doc["sources"])
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
    for town in towns:
        _check_met(town)
    for warning in doc["warnings"]:
        _check_warning(warning)
    need(("met_updated_utc" in doc["sources"]) == any("met" in t for t in towns),
         "sources.met_updated_utc must be present exactly when a town has met")
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
    need(len(body) < MAX_BYTES, f"{len(body)} bytes is not under {MAX_BYTES}")
    return body
