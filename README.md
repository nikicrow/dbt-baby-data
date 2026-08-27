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
           load_to_database.py            load into Postgres
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
  it, and loads it to the database. Use `--zip <path>` for a specific file and
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

Requires Python 3.11+ and a Postgres database — see `scripts/.env.example`
for the connection variables. dbt profile name is `baby_data`.

Run everything through `uv run`. Bare `python` on PATH may be a system
interpreter rather than the project `.venv`, in which case the load step dies
with `pydantic-settings not installed` — after the transform has already
rewritten `seeds/`.

## Running dbt

Data gets into Postgres via `scripts/ingest.py` / `load_to_database.py`
(above), not `dbt seed` — the `seeds/` CSVs are transformed and loaded
directly into the `raw_*` tables that `models/raw` reads as sources.

```bash
cd baby_data
uv run dbt deps          # install dbt-labs/codegen
uv run dbt run
uv run dbt test
```

The source database and schema default to `baby_data` / `public` — where
`load_to_database.py` writes and where the app's Alembic migrations create its
tables. `DBT_SOURCE_DATABASE` and `DBT_SOURCE_SCHEMA` override them if you need
to point at a scratch copy.

## CI

`.github/workflows/dbt-ci.yml` runs `dbt build` on every PR into `main`. **It
needs no repo secrets** — the job is entirely self-contained:

1. Starts an empty `postgres:17` service container named `baby_data`, matching
   the major version of the real database.
2. Replays `ci/source_schema.sql` into it to create the six source tables and
   the enum types they depend on. This step exists because
   `scripts/load_to_database.py` only ever `INSERT`s — it aborts if a table is
   missing and never issues DDL.
3. Runs `scripts/run_pipeline.py`, which transforms the committed `seeds/` CSVs
   and loads them.
4. Runs `dbt deps` and `dbt build --target ci`. If any test fails, the PR check
   goes red.

The container is thrown away with the runner, so nothing needs cleaning up
afterwards.

CI uses its own profile at `ci/profiles.yml`, kept out of `baby_data/` so it
doesn't shadow your local `~/.dbt/profiles.yml`. Every value there defaults to
the service container, and `CI_DB_*` env vars override them if you want to
reproduce a CI run against a local scratch database.

> **`ci/source_schema.sql` is a point-in-time snapshot, currently Alembic head
> `e7a91b4c2d58`.** Regenerate it whenever a migration lands in the app repo,
> or CI will quietly stop testing the real schema. Instructions are in the
> file's header.
