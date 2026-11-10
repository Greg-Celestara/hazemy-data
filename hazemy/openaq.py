"""Community PM2.5 sensors from OpenAQ v3, logged against CAMS in site/internal/bias.csv.

The /v3/locations answer was checked against the live API on 10 Nov 2026: 130 PM2.5 locations in
the box, 78 from AirGradient. Their model code sits in brackets at the end of the instrument name,
e.g. "AirGradient Open Air Generation 1 (O-1PST)"; 32 are "Unknown AirGradient Sensor" with no
code. The measurements endpoint was first exercised by the deploy run that followed.

Pairing convention. CAMS global PM2.5 from the ADS forecast dataset is an instantaneous field at
its valid time T (cams.decode_grib: valid time = run time + step; no accumulation or mean). The
sensor side is therefore the mean of the readings in [T - 1.5 h, T + 1.5 h), the 3 h that T
stands for. The step used is the most recent T whose window has fully ended at run time and
which CAMS covers. A sensor is never compared with a 24 h mean.

A sensor is used only if its newest reading is under 2 h old, it is an outdoor unit, the newest
value is within 0.5-1000 ug/m3, and the last 6 readings hold at least two distinct values.
Sensors more than 20 km from the nearest CAMS cell centre are kept and flagged "far".
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

from hazemy.common import HttpStatusError, PipelineError, fmt, http_get

API = "https://api.openaq.org/v3"
BBOX = "99.5,0.5,119.5,7.5"  # west, south, east, north: Malaysia
LAT_RANGE, LON_RANGE = (0.5, 7.5), (99.5, 119.5)
PM25_PARAMETER_ID = 2
MAX_SENSORS = 40  # keeps a run near 41 calls, under OpenAQ's 60 per minute
CALL_GAP_S = 1.0
FETCH_HOURS = 6
PAGE_LIMIT = 100
MAX_PAGES = 5
MAX_AGE = timedelta(hours=2)
VALUE_RANGE = (0.5, 1000.0)
LAST_READINGS = 6
HALF_STEP = timedelta(hours=1.5)
FAR_KM = 20.0
MAX_CSV_BYTES = 2_000_000
PUBLISHED_BIAS_URL = "https://greg-celestara.github.io/hazemy-data/internal/bias.csv"
ATTRIBUTION = "Data provided by AirGradient via OpenAQ (Licensed under CC-BY 4.0)"
POSITION_DECIMALS = 3
COLUMNS = ["step_utc", "sensor_id", "provider", "contributor", "lat", "lon", "sensor_mean",
           "cams_pm25", "cell_i", "cell_j", "distance_km", "flags", "n_readings"]
MODEL_TOKEN = re.compile(r"\b([OI]-[A-Z0-9][A-Z0-9-]*)")
EARTH_KM = 6371.0


def airgradient_model(location: dict):
    """First `O-...` or `I-...` model code in the instrument names, else the location name.

    OpenAQ puts it in brackets in the instrument name, e.g. "... Generation 1 (O-1PST)".
    None when there is no code, as for "Unknown AirGradient Sensor".
    """
    texts = [i.get("name") for i in location.get("instruments") or []]
    texts.append(location.get("name"))
    for text in texts:
        match = MODEL_TOKEN.search(text or "")
        if match:
            return match.group(1)
    return None


def is_outdoor(location: dict) -> bool:
    model = airgradient_model(location)
    return model is not None and model.startswith("O-")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _get_json(url: str, key: str, label: str, budget, log, http: dict) -> dict:
    body = http_get(url, label=label, budget=budget, log=log, headers={"X-API-Key": key},
                    **http)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as err:
        raise PipelineError(f"{label}: the answer is not JSON ({err}).") from err


def _pm25_sensor_id(location: dict):
    for sensor in location["sensors"]:
        param = sensor["parameter"]
        if param.get("id") == PM25_PARAMETER_ID or param.get("name") == "pm25":
            return sensor["id"]
    return None


def _candidate(location: dict):
    """A dict for one usable outdoor PM2.5 location inside the box, else None."""
    lat = float(location["coordinates"]["latitude"])
    lon = float(location["coordinates"]["longitude"])
    sensor_id = _pm25_sensor_id(location)
    inside = LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]
    if sensor_id is None or not inside or not is_outdoor(location):
        return None
    # Published positions are rounded to about 100 m (Greg, 9 Nov 2026): the sensors sit at
    # private homes, and 100 m is far below the 0.4 degree cell and the 15 km app radius.
    lat, lon = round(lat, POSITION_DECIMALS), round(lon, POSITION_DECIMALS)
    return {"sensor_id": sensor_id, "name": location.get("name"), "lat": lat, "lon": lon,
            "provider": (location.get("provider") or {}).get("name"),
            "contributor": (location.get("owner") or {}).get("name")}


def _reported_recently(location: dict, now: datetime) -> bool:
    """False only when the location says its last reading is over MAX_AGE old."""
    last = (location.get("datetimeLast") or {}).get("utc")
    if not last:
        return True
    try:
        return now - _utc(last) <= MAX_AGE
    except ValueError:
        return True


def fetch_candidates(key: str, now: datetime, budget, log, http: dict) -> list:
    """Outdoor PM2.5 sensors in the box that reported recently, at most MAX_SENSORS.

    Locations whose last reading is too old are dropped before the cap, so the cap is spent on
    sensors that can pass the freshness filter.
    """
    url = f"{API}/locations?bbox={BBOX}&parameters_id={PM25_PARAMETER_ID}&limit=1000"
    doc = _get_json(url, key, "OpenAQ locations", budget, log, http)
    outdoor = [(c, loc) for c, loc in ((_candidate(loc), loc) for loc in doc["results"]) if c]
    found = [c for c, loc in outdoor if _reported_recently(loc, now)]
    found.sort(key=lambda c: c["sensor_id"])
    log(f"OpenAQ: {len(doc['results'])} locations, {len(outdoor)} outdoor PM2.5 sensors, "
        f"{len(found)} reported in the last {MAX_AGE.seconds // 3600} h")
    if len(found) > MAX_SENSORS:
        log(f"OpenAQ: capped at {MAX_SENSORS} of {len(found)} sensors")
        found = found[:MAX_SENSORS]
    return found


def fetch_readings(key: str, sensor_id, now: datetime, budget, log, http: dict) -> list:
    """[(time, value)] oldest first for the last FETCH_HOURS, paging until a short page."""
    start = now - timedelta(hours=FETCH_HOURS)
    out = []
    for page in range(1, MAX_PAGES + 1):
        url = (f"{API}/sensors/{sensor_id}/measurements?datetime_from={fmt(start)}"
               f"&datetime_to={fmt(now)}&limit={PAGE_LIMIT}&page={page}")
        results = _get_json(url, key, f"OpenAQ sensor {sensor_id}", budget, log,
                            http)["results"]
        out.extend(_parse_reading(item, sensor_id) for item in results)
        if len(results) < PAGE_LIMIT:
            break
    else:
        log(f"OpenAQ sensor {sensor_id}: stopped at {MAX_PAGES} pages of readings")
    return sorted(out)


def _parse_reading(item: dict, sensor_id) -> tuple:
    value = item["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(f"OpenAQ sensor {sensor_id}: reading {value!r} is not a number.")
    return _utc(item["period"]["datetimeTo"]["utc"]), float(value)


def passes_filters(readings: list, now: datetime) -> bool:
    """The four use rules: fresh, plausible newest value, not stuck."""
    if not readings:
        return False
    newest_time, newest = readings[-1]
    fresh = timedelta(0) <= now - newest_time < MAX_AGE
    plausible = VALUE_RANGE[0] <= newest <= VALUE_RANGE[1]
    moving = len({value for _, value in readings[-LAST_READINGS:]}) >= 2
    return fresh and plausible and moving


def collect_used(key: str, now: datetime, budget, log, http: dict) -> list:
    """Candidates that pass the filters, each with its readings, `pm25` and `measured`."""
    sleep = http.get("sleep", time.sleep)
    used = []
    for n, cand in enumerate(fetch_candidates(key, now, budget, log, http)):
        if n:
            sleep(CALL_GAP_S)
        readings = fetch_readings(key, cand["sensor_id"], now, budget, log, http)
        if passes_filters(readings, now):
            used.append({**cand, "readings": readings, "pm25": readings[-1][1],
                         "measured": readings[-1][0]})
    log(f"OpenAQ: {len(used)} sensors used")
    return used


def sensors_block(used: list):
    """The v2 `sensors` block, or None when no sensor was used."""
    if not used:
        return None
    return {"openaq_updated_utc": fmt(max(s["measured"] for s in used)),
            "openaq_attribution": ATTRIBUTION,
            "sensors": [{"lat": s["lat"], "lon": s["lon"], "name": s["name"],
                         "contributor": s["contributor"], "provider": s["provider"],
                         "pm25": round(s["pm25"], 1), "measured_utc": fmt(s["measured"])}
                        for s in used]}


def latest_step(now: datetime, series: dict):
    """Newest CAMS valid time T with T + 1.5 h <= now that `series` covers, else None."""
    ready = [t for t in series if t + HALF_STEP <= now]
    return max(ready) if ready else None


def step_mean(readings: list, step: datetime) -> tuple:
    """(mean, count) of the in-range readings in [step - 1.5 h, step + 1.5 h); (None, 0) if none."""
    values = [v for t, v in readings
              if step - HALF_STEP <= t < step + HALF_STEP and VALUE_RANGE[0] <= v <= VALUE_RANGE[1]]
    return (sum(values) / len(values), len(values)) if values else (None, 0)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def nearest_cell(lat: float, lon: float, cells: list) -> tuple:
    """(index into `cells`, km) of the cell centre nearest to the point."""
    dists = [distance_km(lat, lon, c[2], c[3]) for c in cells]
    k = min(range(len(cells)), key=dists.__getitem__)
    return k, dists[k]


def bias_rows(used: list, cells: list, series: dict, now: datetime) -> list:
    """One CSV row (list of str) per used sensor with readings in the latest complete step."""
    step = latest_step(now, series)
    if step is None:
        return []
    rows = []
    for s in used:
        mean, count = step_mean(s["readings"], step)
        k, km = nearest_cell(s["lat"], s["lon"], cells)
        cams = float(series[step][k])
        if mean is None or math.isnan(cams):
            continue
        rows.append([fmt(step), str(s["sensor_id"]), s["provider"] or "", s["contributor"] or "",
                     f"{s['lat']:.3f}", f"{s['lon']:.3f}", f"{mean:.1f}", f"{cams:.1f}",
                     str(cells[k][0]), str(cells[k][1]), f"{km:.1f}",
                     "far" if km > FAR_KM else "", str(count)])
    return rows


def parse_csv(text: str) -> list:
    """Data rows of a bias.csv; raises PipelineError unless the header is exactly COLUMNS."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0] != COLUMNS:
        raise PipelineError("bias.csv does not start with the expected header row.")
    if any(len(r) != len(COLUMNS) for r in rows[1:]):
        raise PipelineError("bias.csv has a row with the wrong number of columns.")
    return rows[1:]


def render_csv(rows: list) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return out.getvalue()


def merge_rows(old: list, new: list) -> list:
    """`old` then `new`, one row per (step_utc, sensor_id); the later row replaces the earlier.

    A later run may have ingested more readings for the same step, so it wins.
    """
    merged = {}
    for row in old + new:
        merged[(row[0], row[1])] = row
    return list(merged.values())


def cap_csv(rows: list, limit: int = MAX_CSV_BYTES) -> str:
    """The CSV text under `limit` bytes, dropping the oldest calendar month(s) of step_utc."""
    while len(render_csv(rows).encode()) > limit and rows:
        oldest = min(r[0][:7] for r in rows)
        rows = [r for r in rows if r[0][:7] != oldest]
    return render_csv(rows)


def _published_rows(budget, log, http: dict) -> tuple:
    """(rows or None, whether the server answered 404)."""
    try:
        body = http_get(PUBLISHED_BIAS_URL, label="published bias.csv", budget=budget, log=log,
                        **http)
        return parse_csv(body.decode("utf-8")), False
    except HttpStatusError as err:
        log(f"WARNING: published bias.csv unavailable: {err}")
        return None, err.status == 404
    except (PipelineError, UnicodeDecodeError) as err:
        log(f"WARNING: published bias.csv unusable: {err}")
        return None, False


def _cached_rows(cache_path: str, log):
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, encoding="utf-8") as fh:
            return parse_csv(fh.read())
    except (OSError, PipelineError) as err:
        log(f"WARNING: cached bias.csv unusable: {err}")
        return None


def load_history(cache_path: str, budget, log, http: dict):
    """Existing rows, or None when no safe starting point exists.

    Both the published copy and the cache copy are read; the one with the newer last step_utc
    wins, the published copy on a tie. A new file starts only if the published copy answered
    HTTP 404 and there is no cache. Any other failure returns None so that a network blip can
    never replace the history with an empty file.
    """
    published, not_found = _published_rows(budget, log, http)
    cached = _cached_rows(cache_path, log)
    found = [r for r in (published, cached) if r is not None]
    if found:
        return max(found, key=lambda rows: max((r[0] for r in rows), default=""))
    if not_found:
        log("bias.csv not published yet (HTTP 404) and no cache: starting a new file")
        return []
    log("ERROR: bias.csv history not found anywhere and the published copy did not answer "
        "404, so no bias.csv is written this run.")
    return None


def store_bias(new_rows: list, out_bias: str, cache_dir: str, budget, log, http: dict) -> None:
    """Merge `new_rows` into the history and write the capped file and its cache copy."""
    cache_path = os.path.join(cache_dir, "bias.csv")
    history = load_history(cache_path, budget, log, http)
    if history is None:
        return
    text = cap_csv(merge_rows(history, new_rows))
    for path in (out_bias, cache_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(path + ".tmp", path)
    log(f"wrote {out_bias} ({len(text)} bytes, {len(new_rows)} new rows offered)")

