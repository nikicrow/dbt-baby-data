# Supabase Migration Plan

## Overview

This project tracks baby data (feeding, sleep, diapers, growth) for Ember and Imogen.
It uses dbt on top of PostgreSQL. Previously the only target was a local PostgreSQL instance.
This migration adds Supabase (hosted PostgreSQL) as a second dbt target while keeping local as the default.

---

## Before First Run — Action Items

These aren't code changes but they're required before anything works:

1. **Add `pydantic-settings` to dependencies.** `pyproject.toml` currently lists `pydantic>=2.0` but not `pydantic-settings`. Add it:
   ```toml
   dependencies = [
       "dbt-core>=1.11.6",
       "dbt-postgres>=1.10.0",
       "pydantic>=2.0",
       "pydantic-settings>=2.0",
       "psycopg2-binary>=2.9",
   ]
   ```
   Then `uv sync`. Without this, `load_to_database.py` will `ImportError` on startup.

2. **Bootstrap the Supabase schema and tables.** `load_to_database.py` expects the target tables to already exist — it only inserts. The first time you point at Supabase, run dbt to create the schema and seed structure:
   ```
   dbt seed --target supabase
   dbt run  --target supabase
   ```
   After that, `python baby_data/scripts/ingest.py --target supabase` can refresh the data.

3. **Know your schemas.** The two targets write to *different* schemas:

   | Target    | dbt (`profiles.yml`) | Python loader default |
   |-----------|----------------------|-----------------------|
   | local     | `models`             | `public` (via `DB_SCHEMA`) |
   | supabase  | `baby_data`          | `baby_data` (via `SUPABASE_DB_SCHEMA`) |

   Local has a pre-existing mismatch between dbt (`models`) and the loader (`public`). If you want them aligned, set `DB_SCHEMA=models` in `.env`. Supabase is consistent.

---

## Changes Summary

### `~/.dbt/profiles.yml`
- Renamed the `dev` output to `local` to make the default target explicit.
- Kept default `target: local` so all existing dbt commands continue to work unchanged.
- Added `supabase` output that reads all credentials from environment variables (`SUPABASE_DB_HOST`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD`, `SUPABASE_DB_NAME`).
- Supabase target uses `sslmode: require` and tuned `keepalives_idle: 0` / `connect_timeout: 10` for a hosted connection.
- Supabase target writes to the `baby_data` schema (vs. `models` for local).

### `baby_data/scripts/load_to_database.py`
- Replaced the ad-hoc `os.environ` config reading with two `pydantic-settings` `BaseSettings` classes: `LocalDatabaseConfig` (reads `DB_*` vars) and `SupabaseDatabaseConfig` (reads `SUPABASE_DB_*` vars). Both load from `baby_data/scripts/.env`.
- Added a `--target` CLI flag (`local` or `supabase`) which selects which config class to instantiate.
- Added `get_connection_string()` helper that builds a `postgresql://user:pass@host:port/db?sslmode=...` URL from the config.
- `schema` is now carried on the config object and threaded through `insert_table`, `check_table_exists`, `get_table_row_count`, and `clear_table` instead of being re-read from env on every call.
- All insert/clear/check logic is otherwise unchanged.

### `baby_data/scripts/ingest.py`
- Added `--target {local,supabase}` flag that is passed through to `load_to_database.py` during the load step.
- Default remains `local` so existing invocations (`python ingest.py`) behave identically.
- Load-step header and completion message now print the active target.

### `baby_data/scripts/.env.example`
- Overwrote the previous `DATABASE_URL`-style example with separate `DB_*` (local) and `SUPABASE_DB_*` (hosted) variable blocks.
- Copy to `baby_data/scripts/.env` and fill in real values (file is gitignored).

### `.gitignore`
- Added `.env` so credentials are never committed. The pattern matches `.env` at any depth.

---

## How to Use

### Local target (default)
```bash
dbt run                           # uses local target by default
dbt test
python baby_data/scripts/ingest.py
python baby_data/scripts/load_to_database.py
```

### Supabase target
```bash
dbt seed  --target supabase       # first-time setup: load seeds
dbt run   --target supabase       # first-time setup: create tables
dbt test  --target supabase
python baby_data/scripts/load_to_database.py --target supabase
python baby_data/scripts/ingest.py --target supabase   # full pipeline into Supabase
```

Every dbt command (`run`, `test`, `seed`, `build`, `snapshot`, …) needs `--target supabase` to point at Supabase — otherwise it falls back to `local`.

---

## Environment Variable Setup

1. **Copy the example file.**

   Linux/macOS:
   ```bash
   cp baby_data/scripts/.env.example baby_data/scripts/.env
   ```

   Windows PowerShell:
   ```powershell
   Copy-Item baby_data\scripts\.env.example baby_data\scripts\.env
   ```

2. **Fill in the values in `.env`.**

   **Local PostgreSQL** — only `DB_PASSWORD` is usually needed if Postgres trusts local connections. Set `DB_SCHEMA=models` if you want the loader to match the dbt `local` target's schema.

   **Supabase** — find your credentials in the Supabase dashboard under *Project Settings → Database*:
   - `SUPABASE_DB_HOST` — looks like `db.xxxxxxxxxxxx.supabase.co`
   - `SUPABASE_DB_USER` — typically `postgres`
   - `SUPABASE_DB_PASSWORD` — your project database password
   - `SUPABASE_DB_NAME` — typically `postgres`

3. **Export the variables.** The Python loader reads `.env` automatically via `pydantic-settings`, but **dbt does not** — it only sees the shell environment. Export them before running dbt:

   Linux/macOS:
   ```bash
   set -a; source baby_data/scripts/.env; set +a
   ```

   Windows PowerShell:
   ```powershell
   Get-Content baby_data\scripts\.env | ForEach-Object {
     if ($_ -match '^\s*([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), 'Process') }
   }
   ```

   Or use `direnv` / `python-dotenv` / a wrapper script to load them automatically per-shell.
