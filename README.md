# dbt-baby-data

The dbt backend for my baby-tracking app (see repo baby-data-app-2025). Ember and Imogen's sleep, feeds,
diapers, growth, and health events go in one end; a `marts.mart_daily_metrics`
table the frontend can query comes out the other, plus fact tables for sleep
sessions and wake windows.

## How it fits together

```
csv export (zip)  ─┐
                    ▼
              scripts/ingest.py         extract + transform + load
                    │
                    ▼
          seeds/*.csv  →  transform_seeds.py  →  transformed_data/*.csv
                    │
                    ▼
           load_to_database.py            load into Postgres/Supabase
                    │
                    ▼
              raw.raw_*                   dbt sources (models/raw)
                    │
                    ▼
              stg_*                       cleaned staging models
                    │
                    ▼
    fct_sleep_sessions, fct_wake_windows, mart_daily_metrics   (marts schema)
                    │
                    ▼
              frontend app reads `marts` directly
```

Two entry points for getting data in:

- **`python scripts/ingest.py`** — the normal path. Finds the latest
  `csv*.zip` export in `~/Downloads`, extracts it into `seeds/`, transforms
  it, and loads it to the database. Use `--zip <path>` for a specific file,
  `--target supabase` to load into Supabase instead of local Postgres, and
  `--skip-load` to transform without touching the database.
- **`python scripts/run_pipeline.py`** — transform + load without the
  zip/extract step, for when `seeds/` is already populated. Supports
  `--baby <name>`, `--dry-run`, `--skip-load`.

Adding a new baby: add an entry to `BABIES` in `scripts/transform_seeds.py`
and drop their CSV exports into `seeds/` as `{Name}_diaper.csv`,
`{Name}_sleep.csv`, etc.

## Project layout

```
baby_data/
├── models/
│   ├── raw/       # dbt sources — one raw_* view per source table
│   ├── staging/   # stg_* — cleaned/typed, one row per event
│   └── marts/     # fct_sleep_sessions, fct_wake_windows, mart_daily_metrics
├── seeds/         # raw CSV exports, one file per baby per event type
├── scripts/       # ingest.py, transform_seeds.py, load_to_database.py, run_pipeline.py
├── macros/        # generate_schema_name — marts land in a schema literally named `marts`
├── tests/         # custom data tests (assert_*.sql)
└── dbt_project.yml
```

## Marts

- **`mart_daily_metrics`** — one row per baby per day: sleep, naps, wake
  windows, feeds, diapers. This is what the frontend's Compare tab reads;
  join on `age_days`/`age_weeks` for age-aligned comparison between babies.
- **`fct_sleep_sessions`** — every sleep session classified as night or nap
  (night = starts 7pm–7am, or 6–7pm if longer than 3h), with night segments
  attributed to the date the night started.
- **`fct_wake_windows`** — daytime awake gaps between consecutive sleeps,
  bounded to 5–360 minutes to exclude tracking gaps and split log entries.

See each model's `.yml` for full column docs.

## Setup

```bash
uv sync
cp baby_data/scripts/.env.example baby_data/scripts/.env   # fill in DB credentials
```

Requires Python 3.11+ and a Postgres database (local or Supabase) — see
`scripts/.env.example` for the connection variables. dbt profile name is
`baby_data`.

## Running dbt

Data gets into Postgres via `scripts/ingest.py` / `load_to_database.py`
(above), not `dbt seed` — the `seeds/` CSVs are transformed and loaded
directly into the `raw_*` tables that `models/raw` reads as sources.

```bash
cd baby_data
dbt deps          # install dbt-labs/codegen
dbt run
dbt test
```

Against Supabase, override the source location with:
`DBT_SOURCE_DATABASE=postgres DBT_SOURCE_SCHEMA=baby_data`.
