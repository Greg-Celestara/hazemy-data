# hazemy.json — public data contract (v2)

Served at `https://greg-celestara.github.io/hazemy-data/v2/hazemy.json`. `v1/hazemy.json` keeps
being published unchanged (schema.md) for installed v1 apps. Same job, same hourly run.
Nothing in it identifies a user. Model values are **model estimates**; Singapore `nea` values are
**measured** by NEA.

v2 = every v1 field (same meaning, same rules as schema.md) plus the additions below.
`"version": 2`.

`sources.firms_updated_utc` is the v1 field (see schema.md): the fetch time of the detections used
for `fires_72h`, `fires_daily`, `fire_bins` and the `path_fire_*` driver fields, or `null` when
FIRMS was down and no saved copy under 24 h old existed. In that case those fields are as for
zero detections (empty lists; `path_fire_count_72h` 0 with null province and trend).

```json
{
  "version": 2,
  "generated_utc": "...", "sources": { "...v1 fields...": "",
    "firms_updated_utc": "2026-10-20T03:35:00Z",
    "gfs_traj_cycles_utc": ["2026-10-18T00:00:00Z", "..."],
    "nea_updated_utc": "2026-10-20T03:00:00Z",
    "met_updated_utc": "2026-10-23T10:29:00Z",
    "nea_attribution": "Contains information from PSI accessed on 20 Oct 2026 from data.gov.sg which is made available under the terms of the Singapore Open Data Licence version 1.0 https://data.gov.sg/open-data-licence" },
  "grid": { "...": "v1" },
  "cells": [
    { "i": 6, "j": 5,
      "now": { "pm25_24h": 69.1, "api": 158, "band": 3,
        "weather": { "temp_c": 31.2, "rh": 74, "feels_c": 38, "rain_mm_3h": 0.4 } },
      "tomorrow": { "api": 121, "api_min": 121, "api_max": 155, "band": 3 },
      "days": [
        { "date": "2026-10-21", "api": 121, "api_min": 121, "api_max": 155, "band": 3,
          "wind850": { "from_deg": 205, "speed_kmh": 26 },
          "weather": { "temp_min_c": 25.1, "temp_max_c": 32.4, "feels_max_c": 41, "rain_mm": 3.2 } },
        { "date": "2026-10-22", "api": 98, "api_min": 90, "api_max": 121, "band": 2,
          "wind850": { "from_deg": 190, "speed_kmh": 20 } },
        { "date": "2026-10-23", "api": 80, "api_min": 70, "api_max": 98, "band": 2,
          "wind850": null }
      ],
      "wind_today": { "from_deg": 162, "from_compass": "S", "speed_kmh": 2 },
      "sg": { "tomorrow_psi": 118, "tomorrow_psi_min": 110, "tomorrow_psi_max": 140, "band": 3,
        "days_psi": [ { "date": "2026-10-21", "psi": 118, "psi_min": 110, "psi_max": 140, "band": 3 } ] },
      "drivers": {
        "rain_tonight_mm": 6.2,
        "wind850_today": { "from_deg": 200, "speed_kmh": 18 },
        "wind850_tomorrow": { "from_deg": 205, "speed_kmh": 26 },
        "path_fire_province": "Central Kalimantan",
        "path_fire_count_72h": 412,
        "path_fire_trend": "rising"
      },
      "path48h": [[3.14, 101.69], [3.0, 101.9], "... 17 points, 3-hourly, newest first"],
      "today_weather": { "temp_min_c": 27.3, "temp_max_c": 32.4, "feels_max_c": 41, "rain_mm": 1.1 }
    }
  ],
  "fires_72h": [ "...v1..." ],
  "fires_daily": [ { "province": "Central Kalimantan", "country": "ID", "counts": [6100, 7800, 9308] } ],
  "fire_bins": [ [-2.125, 113.875, 412] ],
  "nea": { "updated_utc": "...", "regions": {
      "north":   { "lat": 1.41, "lon": 103.82, "psi_24h": 64, "band": 2 },
      "south": {}, "east": {}, "west": {}, "central": {} } },
  "towns": [ { "name": "Petaling Jaya", "lat": 3.107, "lon": 101.606, "country": "MY",
               "met": { "place": "Petaling", "id": "Ds057", "km": 3.2, "days": [
                 { "date": "2026-10-23", "morning": "Tiada Hujan", "afternoon": "Ribut petir di beberapa tempat",
                   "night": "Tiada Hujan", "summary": "Ribut petir di beberapa tempat", "when": "Petang",
                   "min_c": 24, "max_c": 33 } ] } },
             { "name": "Tampines", "lat": 1.353, "lon": 103.945, "country": "SG", "nea_region": "east" } ],
  "warnings": [ { "id": "3f2a9c01b7de", "type": "thunderstorm", "level": null, "marine": false,
      "issued": "2026-10-23T17:55:00+08:00", "valid_from": "2026-10-23T17:00:00+08:00",
      "valid_to": "2026-10-23T21:00:00+08:00", "title_en": "Thunderstorms Warning",
      "title_bm": "Amaran Ribut Petir", "text_en": "...", "text_bm": "...",
      "instruction_en": null, "instruction_bm": null, "states": ["Kedah", "Penang", "Perak"] } ]
}
```

## Rules for the additions

- **Coverage.** Cells are listed if their centre is within ~50 km of Malaysian **or Singapore**
  land. Grid unchanged.
- **`towns`.** Every town gets `country` ("MY" or "SG"). Add ~20 Singapore planning areas
  (hand-compiled, centroid coordinates, each with `nea_region` = north/south/east/west/central,
  following NEA's region split). Malaysian towns unchanged.
- **`nea`.** From `https://api-open.data.gov.sg/v2/real-time/api/psi` (no key), latest item:
  `psi_twenty_four_hourly` per region, plus `regionMetadata` lat/lon. `band` by PSI range:
  1 0–50, 2 51–100, 3 101–200, 4 201–300, 5 >300. If NEA fails, omit `nea` (the app then shows
  the model PSI for Singapore and says so). Attribution string exactly as above, date = access date.
- **`sg`** (only on cells within ~30 km of Singapore land): tomorrow as PSI from the same CAMS
  24 h means as `tomorrow`, using NEA's official PM2.5 sub-index breakpoints. The breakpoints
  must be taken from an NEA/government source and cited in a comment with the URL. If they can't
  be verified, omit `sg` and report it. Method (NEA, same as the app's Psi.kt): round the
  concentration to an integer; segments 0–12, 13–55, 56–150, 151–250, 251–350, 351–500 map to
  PSI 0–50, 51–100, 101–200, 201–300, 301–400, 401–500, linear within a segment, rounded half
  up; above 500 the PSI is 500 (40 ug/m3 -> 83).
- **`days`** (every cell, 1 to 3 entries, one per MYT calendar day: tomorrow, day +2, day +3;
  an MYT day runs 16:00 UTC to 16:00 UTC). `date` is the MYT date. `api`, `api_min`, `api_max`,
  `band` are computed exactly as `tomorrow` (24 h mean, rolling 24 h min/max), so `days[0]`
  equals `tomorrow` and `days[0].wind850` equals `drivers.wind850_tomorrow`. `wind850` is the
  GFS 850 hPa vector mean over that day (as `wind850_today`), or `null` when GFS has no usable
  data for it. A day CAMS cannot cover completely is left out: the list is shorter, never padded
  or extrapolated, and only trailing days are ever missing. Cells' `days` all have the same
  dates. Existing fields (`tomorrow`, `drivers.wind850_tomorrow`) are unchanged for older apps.
  The job asks CAMS for leads 0 to 111 h of the 00 UTC run in use (day +3 ends at most 111 h
  after it; CAMS publishes to 120 h).
- **`sg.days_psi`**: same days as `days` (a prefix of its dates), each
  `{date, psi, psi_min, psi_max, band}` by the NEA method above. `tomorrow_psi*` unchanged.
- **`drivers`** (every cell):
  - `rain_tonight_mm`: GFS total precipitation, 18:00 today to 06:00 tomorrow MYT, at the cell,
    1 decimal.
  - `wind850_today` / `wind850_tomorrow`: GFS 850 hPa wind, vector mean over the MYT day,
    direction FROM (deg, integer), speed km/h integer.
  - `path_fire_count_72h`: FIRMS detections (same 72 h set as `fires_72h`) within 25 km of any
    point of `path48h`. `path_fire_province`: the province holding most of them, or null if
    the count is 0. `path_fire_province` may be null when detections are outside listed provinces
    (count > 0, province null, e.g. all detections at sea). `path_fire_trend`: compare those detections' counts on the last 24 h vs the
    24 h before: "rising" if > 1.2×, "falling" if < 0.8×, else "steady"; null if count is 0.
- **`path48h`**: back-trajectory of the air arriving at the cell centre now, at 850 hPa,
  48 h back, 17 points every 3 h (index 0 = cell centre at the generation time rounded down to
  the hour, so index 8 = 24 h and index 16 = 48 h before that), lat/lon rounded to 2 decimals.
  Method: kinematic, GFS 850 hPa u/v slices every 3 h, bilinear in space, linear in time, 1 h
  integration steps (RK2). Slices: f000 and f003 of the 9 most recent completed cycles
  (`gfs_traj_cycles_utc`), plus forecast steps (f006, f009, f012) of the newest cycle up to the
  first at or after the start. A cycle is published about 5 h late, so the newest cycle lies 5
  to 10 h before "now": the newest hours use forecast steps, not analyses. Nine cycles are
  needed because the path reaches back to 43 h before the newest cycle at worst and 8 cycles
  (f000/f003) only reach 42 h. If the newest cycle's files are not all on the bucket, the cycle
  before it is used for the whole wind set. Stop early (shorter list) if the path leaves
  90–125 E, 12 S–14 N. GFS GRIB data per cycle is cached like CAMS so each cycle is downloaded
  once.
- **`fires_daily`**: same provinces as `fires_72h` (top 5), `counts` = [48–72 h ago,
  24–48 h ago, last 24 h].
- **`fire_bins`**: all 72 h detections in the FIRMS bbox binned to 0.25° (bin centre lat,
  lon, count), at most 1500 bins (largest counts kept).
- **GFS weather (v1.4).** Every number is a GFS model estimate at the cell centre (0.5 degree
  grid, bilinear), from the same cycle as the paths (`gfs_traj_cycles_utc`, last entry), else the
  cycle before it. Fields are added after the existing keys of each object; nothing existing
  changes.
  - `cells[].now.weather`: `{temp_c, rh, feels_c, rain_mm_3h}` at the forecast step nearest to the
    run time. `temp_c` 1 decimal (deg C, 2 m), `rh` integer %, `feels_c` integer deg C,
    `rain_mm_3h` 1 decimal (rain in the 3 h up to that step).
  - `cells[].today_weather`: `{temp_min_c, temp_max_c, feels_max_c, rain_mm}` for the rest of
    today's MYT day: the steps from the one nearest to the run time to the end of the day for
    temperature, rain from the run time to the end of the day.
  - `cells[].days[i].weather`: the same four fields over the whole MYT day (16:00 UTC to 16:00
    UTC); `rain_mm` is the accumulation across exactly those two instants. Day min and max use
    the 3-hourly steps inside the day.
  - **Feels like** is the NOAA heat index (Rothfusz regression with NOAA's adjustments, from
    https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml). Below 27 deg C it equals the
    temperature. Whole deg C.
  - A forecast hour that could not be had leaves the fields that need it `null`, never an
    estimate: a day with one missing temperature step has `temp_min_c`, `temp_max_c` and
    `feels_max_c` null but may still have `rain_mm`. If no GFS weather can be had at all, every
    weather field is null and the rest of v2 is unaffected.
  - Source: the NOMADS grid filter (`filter_gfs_0p50.pl`, subregion 94-122 E, 4 S-10 N), one
    request per forecast hour, about 10 KB each, cached with the other GFS files (kind `wx`).
- **MetMalaysia forecast (v1.4).** One request per run to
  `https://api.data.gov.my/weather/forecast/`. Each Malaysian town takes the nearest MetMalaysia
  place of type Tn (town) or Ds (district) within 25 km (coordinates in `data/met_places.json`, from GeoNames (CC BY 4.0), built by
  `scripts/build_met_places.py`; attribute GeoNames next to MetMalaysia).
  Rc, Dv and St places are never used. No match, or a Singapore town, gives no `met`.
  `towns[].met` = `{place, id, km, days}`; `days` is today plus 3 days in date order (MYT dates),
  only dates the feed has. Day fields are copied from the feed: `morning`, `afternoon`, `night`,
  `summary` and `when` are its Malay text unchanged, `min_c` and `max_c` its integers. If the
  fetch fails or returns 0 rows, the last good copy is reused for up to 24 h, then `met` is
  dropped. `sources.met_updated_utc` is the fetch time of the data used, present only when at
  least one town has `met`.
- **`warnings` (v1.4).** Top level, always present, a list (empty when none). From
  `https://api.data.gov.my/weather/warning/`. A warning is kept when its `valid_to` is later
  than now; "No Advisory" / "Tiada Nasihat" rows, rows with null validity and expired rows are
  dropped. The feed's times carry no offset and are MYT, so `+08:00` is added. Fields:
  - `id`: first 12 hex digits of the SHA-1 of `title_en` + `issued` + `valid_from` (the two
    times as written in this file, with the offset).
  - `type`, from the title: `thunderstorm`, `heavy_rain`, `strong_wind`, `heat`, `cyclone`,
    `other` (checked in that order).
  - `marine`: true when the title or heading says "Rough Seas" / "Laut Bergelora", or the English
    text says "waters of".
  - `level`: from the title and heading only: `cat1`/`cat2`/`cat3` (FIRST/SECOND/THIRD CATEGORY),
    `heat1`/`heat2`/`heat3` (heat warnings stating a level 1-3), else `danger` (Danger, Bahaya),
    `severe` (Severe, Buruk), `alert` (Alert, Waspada), else `null`.
  - `title_*`, `text_*`, `instruction_*`: copied from the feed (instruction may be null).
  - `states`: canonical names of the states and federal territories named in `text_en`, then any
    only named in `text_bm`, in order of appearance. Names: Johor, Kedah, Kelantan, Melaka,
    Negeri Sembilan, Pahang, Penang, Perak, Perlis, Sabah, Sarawak, Selangor, Terengganu, Kuala
    Lumpur, Putrajaya, Labuan. Aliases: Pulau Pinang = Penang, N. Sembilan = Negeri Sembilan,
    Malacca = Melaka. An empty list means the text names none (treat as all of Malaysia).
    Marine warnings list states by the waters they name.
  - If the fetch fails, the last good copy is kept, still filtered by `valid_to`.
- Neither MetMalaysia feed nor GFS weather can stop v2 from being published.
- File under 700 KB (`MAX_BYTES` in `output_v2.py`, enforced by the job; v1 stays under 500 KB). Breaking changes go to `/v3/`.
