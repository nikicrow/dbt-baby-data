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

## Connecting to the database on fedora-1

The authoritative database lives on `fedora-1`, the home server on the tailnet.
It is **not** Postgres on that host — there is no `psql` there — but a rootless
podman container, `baby-data-postgres` (`postgres:17-alpine`), publishing
`127.0.0.1:5433`. Because it binds to loopback, nothing on the tailnet can
reach port 5432 or 5433 directly; you tunnel over SSH.

```bash
ssh -N fedora-1-db      # leave running; Ctrl+C closes it
```

That alias comes from `~/.ssh/config` and forwards laptop port 5433 to the
container:

```
Host fedora-1-db
    HostName fedora-1
    User crowclaws
    IdentityFile ~/.ssh/id_ed25519
    LocalForward 5433 127.0.0.1:5433
    ServerAliveInterval 60
    ExitOnForwardFailure yes
```

With the tunnel open, the database is `localhost:5433`, database `baby_data`.
Two roles: `readonly` (SELECT on `public`, `marts` and `ml` — use this for
pgAdmin, VS Code and ad-hoc SQL) and `postgres` (superuser, for dbt builds).
Passwords are not in this repo: the superuser's is `POSTGRES_PASSWORD` in
`podman inspect baby-data-postgres`, and both are in the `FEDORA_DB_PASSWORD`
and `FEDORA_DB_RO_PASSWORD` env vars on the laptop.

`~/.dbt/profiles.yml` has a target for each, plus the laptop's stale copy. It
deliberately declares **no default target**, so dbt fails asking for `-t`
rather than rebuilding something you didn't mean:

```bash
uv run dbt build --exclude resource_type:seed -t fedora_via_tunnel   # writes the real tables
uv run dbt show --inline "select * from ml.ml_sleep_training_set" --limit 20 -t fedora_readonly
```

Note `dbt show` appends its own `limit`, so pass `--limit N` rather than
writing `limit N` into the query — the two collide into a syntax error.

> **Merging a PR does not build anything on `fedora-1`.** CI builds into a
> throwaway container (below), so new models exist in `main` and nowhere else
> until someone runs dbt against the server deliberately. After a build that
> creates a new schema, grant it to the read-only role — grants do not apply to
> schemas that did not exist when they were made:
>
> ```sql
> GRANT USAGE ON SCHEMA <new> TO readonly;
> GRANT SELECT ON ALL TABLES IN SCHEMA <new> TO readonly;
> ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA <new> GRANT SELECT ON TABLES TO readonly;
> ```

Seeds are excluded from builds against the server on purpose. Rows carry a
`source` column, and `source='app'` rows exist only in that Postgres — they
cannot be reproduced from the CSVs. Move the database with
`pg_dump`/`pg_restore`, never by re-running the ingest.

To run SQL on the box itself, without the tunnel:

```bash
ssh crowclaws@fedora-1 "podman exec baby-data-postgres psql -U postgres -d baby_data -c '<sql>'"
```

That box also runs unrelated `buzz-prod_*` and `garmin-notes-*` stacks, each
with its own Postgres container — check the container name before connecting.
Its login shell prints a harmless error about a missing `openclaw.bash`; it is
not a failure.

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
