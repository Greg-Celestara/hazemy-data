"""MetMalaysia forecast per town and weather warnings, from api.data.gov.my.

Both feeds are optional: a failed fetch reuses the last good copy (forecast for up to 24 h,
warnings until they expire) and otherwise leaves the field out. Nothing here stops the job.
The text is copied as the feed gives it (Malay); the app does the wording.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta

from hazemy import grid as gridmod
from hazemy.common import MYT, PipelineError, fmt, http_get, parse_time
from hazemy.firms import CLOCK_SKEW, MAX_SAVED_AGE

FORECAST_URL = "https://api.data.gov.my/weather/forecast/"
WARNING_URL = "https://api.data.gov.my/weather/warning/"
USER_AGENT = "HazeMY-data"
PLACES_PATH = os.path.join(gridmod.DATA_DIR, "met_places.json")
SAVED_NAME = "met_last.json"
MAX_KM = 25.0
FORECAST_DAYS = 4  # today plus 3 days
FETCH_ATTEMPTS = 2
FETCH_TIMEOUT_S = 30
EARTH_KM = 6371.0
DAY_FIELDS = (("date", "date"), ("morning", "morning_forecast"),
              ("afternoon", "afternoon_forecast"), ("night", "night_forecast"),
              ("summary", "summary_forecast"), ("when", "summary_when"),
              ("min_c", "min_temp"), ("max_c", "max_temp"))
STATES = ("Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang", "Penang", "Perak",
          "Perlis", "Sabah", "Sarawak", "Selangor", "Terengganu", "Kuala Lumpur", "Putrajaya",
          "Labuan")
STATE_ALIASES = {"Pulau Pinang": "Penang", "N. Sembilan": "Negeri Sembilan",
                 "N.Sembilan": "Negeri Sembilan", "Malacca": "Melaka"}
NO_ADVISORY = ("no advisory", "tiada nasihat")
TYPE_KEYWORDS = (("thunderstorm", ("thunderstorm", "ribut petir")),
                 ("heavy_rain", ("heavy rain", "hujan lebat", "continuous rain",
                                 "hujan berterusan")),
                 ("strong_wind", ("strong wind", "angin kencang")),
                 ("heat", ("hot weather", "cuaca panas", "heat")),
                 ("cyclone", ("cyclone", "siklon")))
MARINE_WORDS = ("rough seas", "laut bergelora")
CATEGORY = {"first": 1, "second": 2, "third": 3, "pertama": 1, "kedua": 2, "ketiga": 3}


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def load_places(path: str = None) -> list:
    """[{'id', 'name', 'lat', 'lon'}] of the Tn (town) and Ds (district) places."""
    with open(path or PLACES_PATH, encoding="utf-8") as fh:
        places = json.load(fh)["places"]
    return [p for p in places if p["id"][:2] in ("Tn", "Ds")]


def nearest_place(town: dict, places: list):
    """(place, km) of the closest place within MAX_KM of the town, else None."""
    best = min(((km_between(town["lat"], town["lon"], p["lat"], p["lon"]), p["id"], p)
                for p in places), default=None)
    if best is None or best[0] > MAX_KM:
        return None
    return best[2], best[0]


def match_towns(towns: list, places: list) -> list:
    """Per town: (place, km) or None. Singapore towns are never matched."""
    return [nearest_place(t, places) if t.get("country", "MY") == "MY" else None for t in towns]


def parse_forecast(rows: list, wanted: set) -> dict:
    """{place id: {'name', 'days': [...]}} for the wanted ids, days in date order."""
    found = {}
    for row in rows:
        loc = row["location"]
        if loc["location_id"] not in wanted:
            continue
        entry = found.setdefault(loc["location_id"], {"name": loc["location_name"], "days": []})
        entry["days"].append({out: row.get(src) for out, src in DAY_FIELDS})
    for entry in found.values():
        entry["days"].sort(key=lambda d: d["date"] or "")
    return found


def town_met(match, saved: dict, now: datetime):
    """The `met` object of one town, or None when it has no place or the feed lacks it."""
    if match is None:
        return None
    place, km = match
    entry = saved.get(place["id"])
    if entry is None:
        return None
    wanted = {(now.astimezone(MYT) + timedelta(days=k)).date().isoformat()
              for k in range(FORECAST_DAYS)}
    days, seen = [], set()
    for day in entry["days"]:
        if day["date"] in wanted and day["date"] not in seen:  # null or repeated dates dropped
            seen.add(day["date"])
            days.append(day)
    if not days:
        return None
    return {"place": entry["name"], "id": place["id"], "km": round(km, 1), "days": days}


def _fetch_json(url: str, label: str, budget, log, http: dict):
    body = http_get(url, label=label, budget=budget, log=log, headers={"User-Agent": USER_AGENT},
                    attempts=FETCH_ATTEMPTS, timeout=FETCH_TIMEOUT_S, **http)
    try:
        data = json.loads(body)
    except ValueError as err:
        raise PipelineError(f"{label}: reply is not JSON ({err}).") from err
    if not isinstance(data, list):
        raise PipelineError(f"{label}: reply is not a list.")
    return data


# --- warnings -------------------------------------------------------------------------------


def _words(text: str, words: tuple) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", text) for w in words)


def warning_type(title_en: str, title_bm: str) -> str:
    text = f"{title_en} {title_bm}".lower()
    for name, words in TYPE_KEYWORDS:
        if _words(text, words):
            return name
    return "other"


def is_marine(row: dict) -> bool:
    head = " ".join(str(x or "") for x in (row["warning_issue"]["title_en"],
                                           row["warning_issue"].get("title_bm"),
                                           row.get("heading_en"), row.get("heading_bm"))).lower()
    return any(w in head for w in MARINE_WORDS) or "waters of" in str(row.get("text_en")).lower()


def warning_level(row: dict, kind: str):
    """Level from the title and heading (not the body, where 'dangerous' is ordinary text)."""
    issue = row["warning_issue"]
    text = " ".join(str(x or "") for x in (issue["title_en"], issue.get("title_bm"),
                                           row.get("heading_en"), row.get("heading_bm"))).lower()
    cat = (re.search(r"\b(first|second|third)\s+category", text)
           or re.search(r"\bkategori\s+(pertama|kedua|ketiga)", text))
    if cat:
        return f"cat{CATEGORY[cat.group(1)]}"
    heat = re.search(r"\b(?:level|aras|tahap)\s*([123])\b", text)
    if kind == "heat" and heat:
        return f"heat{heat.group(1)}"
    for level, words in (("danger", ("danger", "bahaya")), ("severe", ("severe", "buruk")),
                         ("alert", ("alert", "waspada"))):
        if _words(text, words):
            return level
    return None


def warning_states(text_en, text_bm) -> list:
    """Canonical state names found in the texts, in order of first appearance (English first)."""
    names = {s: s for s in STATES} | STATE_ALIASES
    found = []
    for text in (text_en, text_bm):
        hits = []
        for alias, state in names.items():
            m = re.search(rf"(?<!\w){re.escape(alias.lower())}(?!\w)", str(text or "").lower())
            if m and state not in found and state not in [h[1] for h in hits]:
                hits.append((m.start(), state))
        found.extend(state for _, state in sorted(hits))
    return found


def _with_offset(text):
    """The timestamp with an offset: +08:00 (MYT) is added only when it has none.

    Raises ValueError when the result is not an ISO timestamp.
    """
    if not text:
        return None
    text = str(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif not re.search(r"[+-]\d\d:?\d\d$", text[10:]):
        text += "+08:00"
    datetime.fromisoformat(text)
    return text


def parse_warning(row: dict):
    """The output warning of one feed row, or None for 'No Advisory' rows and null validity."""
    issue = row["warning_issue"]
    title_en = str(issue["title_en"] or "")
    if title_en.lower() in NO_ADVISORY or str(issue.get("title_bm") or "").lower() in NO_ADVISORY:
        return None
    issued, start, end = (_with_offset(x) for x in (issue["issued"], row.get("valid_from"),
                                                    row.get("valid_to")))
    if not (issued and start and end):
        return None
    kind = warning_type(title_en, str(issue.get("title_bm") or ""))
    digest = hashlib.sha1((title_en + issued + start).encode("utf-8")).hexdigest()[:12]
    return {"id": digest, "type": kind, "level": warning_level(row, kind),
            "marine": is_marine(row), "issued": issued, "valid_from": start, "valid_to": end,
            "title_en": title_en, "title_bm": issue.get("title_bm"),
            "text_en": row.get("text_en"), "text_bm": row.get("text_bm"),
            "instruction_en": row.get("instruction_en"),
            "instruction_bm": row.get("instruction_bm"),
            "states": warning_states(row.get("text_en"), row.get("text_bm"))}


def parse_warnings(rows: list, log=print) -> list:
    """Parsed warnings; a row that cannot be parsed is dropped and logged, the rest are kept."""
    out = []
    for row in rows:
        try:
            parsed = parse_warning(row)
        except ValueError as err:
            title = (row.get("warning_issue") or {}).get("title_en")
            log(f"WARNING: dropping MetMalaysia warning {title!r}: bad timestamp ({err})")
            continue
        if parsed is not None:
            out.append(parsed)
    return out


def unexpired(warnings: list, now: datetime) -> list:
    return [w for w in warnings if datetime.fromisoformat(w["valid_to"]) > now]


# --- saved copies ---------------------------------------------------------------------------


def _read_state(cache_dir: str) -> dict:
    try:
        with open(os.path.join(cache_dir, SAVED_NAME), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(cache_dir: str, state: dict, log) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, SAVED_NAME)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except OSError as err:
        log(f"WARNING: could not save MetMalaysia data to {cache_dir}: {err}")


def _saved_forecast(state: dict, now: datetime):
    """(places, fetched) from the saved copy when it is under 24 h old, else None."""
    try:
        fetched = parse_time(state["forecast"]["fetched_utc"])
        places = state["forecast"]["places"]
    except (KeyError, ValueError, TypeError):
        return None
    if not isinstance(places, dict) or not now - MAX_SAVED_AGE < fetched <= now + CLOCK_SKEW:
        return None
    return places, fetched


def refresh_forecast(wanted: set, now: datetime, budget, state: dict, log, http: dict):
    """(places, fetched time) from the feed (saved into `state`), the saved copy, or None."""
    try:
        rows = _fetch_json(FORECAST_URL, "MetMalaysia forecast", budget, log, http)
        if not rows:
            raise PipelineError("MetMalaysia forecast: the feed returned 0 rows.")
        places = parse_forecast(rows, wanted)
    except (PipelineError, KeyError, TypeError) as err:
        log(f"WARNING: MetMalaysia forecast failed: {err}")
        saved = _saved_forecast(state, now)
        if saved is None:
            log("MetMalaysia forecast unavailable and no saved copy under 24 h old: no 'met'")
        else:
            log(f"MetMalaysia forecast unavailable, using the copy from {fmt(saved[1])}")
        return saved
    state["forecast"] = {"fetched_utc": fmt(now), "places": places}
    return places, now


def refresh_warnings(now: datetime, budget, state: dict, log, http: dict) -> list:
    """Current warnings from the feed, else the last good copy; never raises."""
    try:
        warnings = parse_warnings(_fetch_json(WARNING_URL, "MetMalaysia warnings", budget, log,
                                              http), log)
    except (PipelineError, KeyError, TypeError, ValueError) as err:
        log(f"WARNING: MetMalaysia warnings failed: {err}")
        warnings = state.get("warnings", {}).get("items", [])
        log(f"MetMalaysia warnings unavailable, keeping {len(warnings)} saved warning(s)")
    else:
        state["warnings"] = {"fetched_utc": fmt(now), "items": warnings}
    try:
        return unexpired(warnings, now)
    except (KeyError, TypeError, ValueError):
        return []


def load_met(towns: list, now: datetime, budget, cache_dir: str, log=print, **http) -> dict:
    """{'met': [per town object or None], 'updated': datetime or None, 'warnings': [...]}."""
    state = _read_state(cache_dir)
    matches = match_towns(towns, load_places())
    wanted = {m[0]["id"] for m in matches if m}
    loaded = refresh_forecast(wanted, now, budget, state, log, http)
    warnings = refresh_warnings(now, budget, state, log, http)
    _write_state(cache_dir, state, log)
    if loaded is None:
        return {"met": [None] * len(towns), "updated": None, "warnings": warnings}
    places, fetched = loaded
    met = [town_met(m, places, now) for m in matches]
    return {"met": met, "updated": fetched if any(met) else None, "warnings": warnings}
