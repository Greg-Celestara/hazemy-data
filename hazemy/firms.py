"""NASA FIRMS VIIRS NRT active fires: fetch, parse, count by admin-1 province."""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timedelta, timezone

import shapely
from shapely.strtree import STRtree

from hazemy.common import PipelineError, http_get

SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
BBOX = (95, -6, 120, 8)  # west, south, east, north
# The day range counts back from the current UTC date, which is nearly empty just after 00:00
# UTC, so ask for 4 days and cut to 72 h ourselves.
DAY_RANGE = 4
WINDOW_H = 72
TOP_N = 5
COUNTRY_CODES = {"IDN": "ID", "MYS": "MY", "BRN": "BN"}
REQUIRED_COLUMNS = ("latitude", "longitude", "acq_date", "acq_time", "confidence")
CLOCK_SKEW = timedelta(hours=1)


def firms_url(key: str, source: str) -> str:
    west, south, east, north = BBOX
    return (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/"
            f"{west},{south},{east},{north}/{DAY_RANGE}")


def parse_csv(text: str, source: str, now: datetime) -> list:
    """[(lat, lon)] of detections in the last 72 h, low-confidence ones dropped.

    FIRMS answers an empty window with just the header, which gives an empty list. A body
    without the expected columns (e.g. an error message with HTTP 200) raises.
    """
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise PipelineError(f"FIRMS {source}: response lacks column(s) {missing}: "
                            f"{text[:120]!r}")
    oldest, newest = now - timedelta(hours=WINDOW_H), now + CLOCK_SKEW
    points = []
    for line, row in enumerate(reader, start=2):
        try:
            acquired = datetime.strptime(
                f"{row['acq_date']} {int(row['acq_time']):04d}", "%Y-%m-%d %H%M"
            ).replace(tzinfo=timezone.utc)
            lat, lon = float(row["latitude"]), float(row["longitude"])
        except (ValueError, TypeError) as err:
            raise PipelineError(f"FIRMS {source}: bad row at line {line}: {err}") from err
        # 'l' is VIIRS low confidence (mostly sun glint and hot ground); the portal drops it too.
        if row["confidence"].strip().lower() == "l" or not oldest < acquired <= newest:
            continue
        points.append((lat, lon))
    return points


def fetch_detections(key: str, now: datetime, budget, *, log=print, **http) -> list:
    points = []
    for source in SOURCES:
        body = http_get(firms_url(key, source), label=f"FIRMS {source}", budget=budget, log=log,
                        **http)
        found = parse_csv(body.decode("utf-8", "replace"), source, now)
        log(f"FIRMS {source}: {len(found)} detections in the last {WINDOW_H} h")
        points.extend(found)
    return points


def count_by_province(points: list, provinces: list, top: int = TOP_N) -> list:
    """Top `top` provinces by detection count: [{'province', 'country', 'count'}].

    Detections outside every province (sea, other countries) are not counted. Ties sort by name
    so the file is reproducible.
    """
    if not points:
        return []
    geoms = [geom for _, _, geom in provinces]
    tree = STRtree(geoms)
    pts = shapely.points([(lon, lat) for lat, lon in points])
    point_idx, geom_idx = tree.query(pts, predicate="within")
    counts = Counter()
    seen = set()
    for p, g in zip(point_idx.tolist(), geom_idx.tolist()):
        if p in seen:  # border points can sit in two polygons; count each detection once
            continue
        seen.add(p)
        name, country, _ = provinces[g]
        counts[(name, COUNTRY_CODES.get(country, country))] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))[:top]
    return [{"province": name, "country": country, "count": n} for (name, country), n in ranked]
