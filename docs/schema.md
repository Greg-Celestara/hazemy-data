# hazemy.json — public data contract (v1)

Served by GitHub Pages at `https://greg-celestara.github.io/hazemy-data/v1/hazemy.json`.
Written by `.github/workflows/hazemy.yml`. Read by the HazeMY Android app. Nothing in it
identifies a user. Every value is a **model estimate**, not a station reading.

```json
{
  "version": 1,
  "generated_utc": "2026-10-19T10:40:00Z",
  "sources": {
    "cams_run_utc": "2026-10-19T00:00:00Z",
    "firms_fetched_utc": "2026-10-19T10:35:00Z",
    "firms_updated_utc": "2026-10-19T10:35:00Z",
    "gfs_run_utc": "2026-10-19T06:00:00Z",
    "attribution": "Contains modified Copernicus Atmosphere Monitoring Service information 2026; NASA FIRMS; NOAA GFS"
  },
  "grid": { "lat0": 0.8, "lon0": 99.6, "step": 0.4, "nlat": 17, "nlon": 50 },
  "cells": [
    { "i": 0, "j": 0,
      "now":      { "pm25_24h": 22.4, "api": 72, "band": 2 },
      "tomorrow": { "api": 128, "api_min": 115, "api_max": 150, "band": 3 },
      "wind_today": { "from_deg": 225, "from_compass": "SW", "speed_kmh": 15 } }
  ],
  "fires_72h": [ { "province": "South Sumatra", "country": "ID", "count": 340 } ],
  "towns": [ { "name": "Petaling Jaya", "lat": 3.107, "lon": 101.606 } ]
}
```

Rules
- `sources.firms_updated_utc` (added later, additive): when the fire detections in this file were
  fetched. Normally equal to `firms_fetched_utc`. If FIRMS was unreachable, the job reuses its
  last saved detections when they are under 24 h old and this is their fetch time; with no usable
  saved copy `fires_72h` is `[]` and this is `null`. Clients may show "fire data from <time>"
  when it is older than `generated_utc`, and must treat `null` as "no fire data". Older clients
  ignore the field.
- Grid covers Malaysia, lat 0.8–7.6 N, lon 99.6–119.6 E. Only cells whose centre is within
  ~50 km of Malaysian land are listed (`i` = lat index, `j` = lon index).
- `now.pm25_24h`: mean CAMS surface PM2.5 (µg/m³) over the 24 h ending at the latest hour
  covered (analysis + forecast steps from the newest run). `api`: DOE PM2.5 sub-index of it.
- `tomorrow`: tomorrow = calendar day in MYT (UTC+8). `api` from the 24 h mean of that day;
  `api_min`/`api_max` = min/max of the hourly-rolling 24 h API ending inside that day.
- DOE conversion (DOE API_Calculation.pdf, read 19 Oct 2026), linear interpolation,
  thresholds as DOE's own Excel formula: <12.1→0–50 (0–12.0), <50.5→51–100 (12.1–50.4),
  <55.5→101–150 (50.5–55.4), <150.5→151–200 (55.5–150.4), <250.5→201–300 (150.5–250.4),
  <350.5→301–400 (250.5–350.4), <500.5→401–500 (350.5–500.4); above 500.4 → 500.
  Round concentration to 1 decimal before lookup; round API to integer.
- `band`: 1 Baik (0–50), 2 Sederhana (51–100), 3 Tidak Sihat (101–200),
  4 Sangat Tidak Sihat (201–300), 5 Berbahaya (>300).
- `wind_today`: GFS 10 m wind, vector mean over today MYT (00–24), direction it blows FROM,
  8-point compass, speed km/h rounded to integer.
- `fires_72h`: FIRMS VIIRS (S-NPP + NOAA-20 + NOAA-21) NRT detections, last 72 h, bbox
  95–120 E, 6 S–8 N, counted by admin-1 province (Indonesia, Malaysia, Brunei); sorted
  descending; top 5 only; provinces with 0 omitted.
- `towns`: static list of Malaysian towns (population ≥ ~50,000, plus every state capital),
  for the app's on-device "nearest town" label. No geocoding service is ever called.
- File size under 500 KB. Breaking changes go to `/v2/`.
