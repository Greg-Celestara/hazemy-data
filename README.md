# hazemy-data

Scheduled job behind the HazeMY haze app. It builds one public file, `hazemy.json`, and
publishes it with GitHub Pages at
`https://greg-celestara.github.io/hazemy-data/v1/hazemy.json`. The file format is fixed in
[docs/schema.md](docs/schema.md). `site/v2/wind.json` (850 hPa wind for the slider and five days
of fires, optional) is described in [docs/schema-wind.md](docs/schema-wind.md). Every value is a model estimate, not a station reading.

## What the job does

1. **CAMS PM2.5** (Copernicus Atmosphere Data Store, global forecasts, 0.4 degree, 3-hourly):
   downloads the newest 00 UTC run, cropped to the Malaysia box, plus the previous run for the
   hours before the newest one starts. Steps are 3-hourly, so a "24 h mean" is the mean of 8
   steps and the "hourly-rolling" windows in the contract roll in 3-hour steps.
2. **GFS 10 m wind** (NOAA, 0.5 degree, AWS open-data bucket): only the U and V messages, by byte
   range, for today in Malaysian time (MYT, UTC+8). Past times use that time's own analysis,
   later times the newest cycle's forecast. Averaged as vectors.
3. **FIRMS VIIRS fires** (NASA; S-NPP, NOAA-20, NOAA-21 near real time): last 120 h fetched (every
   product uses the last 72 h; wind.json shows all five days), low
   confidence dropped, counted by admin-1 province (`data/provinces.geojson`, geoBoundaries
   gbHumanitarian, CC BY 3.0 IGO), top 5.
4. Converts PM2.5 to the DOE API, assigns bands, validates the result against the contract
   (also: under 500 KB) and writes `site/v1/hazemy.json` atomically. **Any failure exits with
   code 1 before the file is touched**, and the workflow then skips the deploy, so the last good
   file stays live. v1 never depends on v2: v1 is written first, and if building v2 fails the job
   logs the error, copies the currently published v2 from GitHub Pages into `site/v2/` (no v2
   file if that fails too) and still exits 0 so v1 is deployed.

5. **OpenAQ community PM2.5 sensors** (optional; needs the secret `OPENAQ_API_KEY`, sent as the
   `X-API-Key` header and never logged). Malaysian outdoor sensors (AirGradient `O-` models) that
   pass the freshness, range and not-stuck rules become the optional `sensors` block of v2 and
   one row each in `site/internal/bias.csv`: the sensor's mean over a CAMS 3-hour step against
   CAMS PM2.5 for the same step. CAMS PM2.5 is instantaneous at its valid time T, so the sensor
   window is [T - 1.5 h, T + 1.5 h) for the most recent T whose window has ended; never a 24 h
   mean. The file is seeded from the published Pages copy, then `cache/bias.csv`, de-duplicated on
   (step, sensor), capped at 2 MB (oldest month dropped). Without the key, or if OpenAQ fails,
   the step is skipped; v1 and v2 are unaffected and the job still exits 0. The OpenAQ endpoints
   were written from its documentation and have not been seen answering.

Cells: the 0.4 degree grid of the contract, kept only if the cell centre is within 50 km of
Malaysian land. Towns come from `data/towns.json` (source noted inside the file).

## Sources and attribution

`sources.attribution` in the file reads, for 2026 data:

> Contains modified Copernicus Atmosphere Monitoring Service information 2026; NASA FIRMS; NOAA GFS

Keep that text visible wherever the data is shown. Sensor values (v2 `sensors`) must show:

> Data provided by AirGradient via OpenAQ (Licensed under CC-BY 4.0)

with the contributor name where one is published. Province outlines: geoBoundaries
gbHumanitarian (CC BY 3.0 IGO).

## Run locally

Python 3.9 or newer. `eccodes` ships its own library as a wheel.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ADS_API_KEY=...      # Copernicus ADS personal access token
export FIRMS_MAP_KEY=...    # NASA FIRMS MAP_KEY
export OPENAQ_API_KEY=...   # optional: OpenAQ v3 key; without it the sensors step is skipped
.venv/bin/python -m hazemy  # writes site/v1/hazemy.json, caches downloads in cache/
```

A first run waits for the ADS queue (about 5 minutes); later runs on the same day reuse
`cache/`. Total download is a few MB; the job stops at 500 MB.

## Tests

```
.venv/bin/python -m unittest discover -s tests -t .
```

The tests need no network or keys: the network boundaries are replaced by fakes and small GRIB
files are generated with eccodes.

## GitHub setup (once)

- Secrets `ADS_API_KEY` and `FIRMS_MAP_KEY` (Settings, Secrets and variables, Actions).
- Settings, Pages, Source: "GitHub Actions".
- `.github/workflows/hazemy.yml` runs every 30 min (:15 and :45) and on demand
  (cron-job.org calls workflow_dispatch, because GitHub's schedule is unreliable).
