# Supabase Migration Plan

## Overview

This project tracks baby data (feeding, sleep, diapers, growth) for Ember and Imogen.
It uses dbt on top of PostgreSQL. Previously the only target was a local PostgreSQL instance.
This migration adds Supabase (hosted PostgreSQL) as a second dbt target while keeping local as the default.

---

## Changes Summary

### `~/.dbt/profiles.yml`
- Renamed the `dev` output to `local` to make the default target explicit.
- Kept default `target: local` so all existing dbt commands continue to work unchanged.
- Added `supabase` output that reads all credentials from environment variables.
- Supabase target uses `sslmode: require` and tuned `keepalives_idle`/`connect_timeout` for a hosted connection.

### `baby_data/scripts/load_to_database.py`
- Replaced the ad-hoc `os.environ` config reading with a `DatabaseConfig` Pydantic Settings class.
- Added a `--target` CLI flag (`local` or `supabase`). When `--target supabase` is passed the script reads `SUPABASE_DB_*` env vars; otherwise it reads `DB_*` env vars.
- Added `get_connection_string()` helper that builds a `postgresql://` URL from the config.
- All other logic (insert, clear, check) is unchanged.

### `baby_data/scripts/.env.example`
- New file documenting all environment variables required for both targets.
- Copy to `baby_data/scripts/.env` and fill in real values (file is gitignored).

### `.gitignore`
- Added `.env` so credentials are never committed.

---

## How to Use

### Local target (default)
```bash
dbt run                           # uses local target by default
python baby_data/scripts/load_to_database.py
```

### Supabase target
```bash
dbt run --target supabase         # point dbt at Supabase
python baby_data/scripts/load_to_database.py --target supabase
```

---

## Environment Variable Setup

1. Copy the example file:
   ```bash
   cp baby_data/scripts/.env.example baby_data/scripts/.env
   ```

2. Fill in the values in `.env`:

   **Local PostgreSQL** – only `DB_PASSWORD` is usually needed if Postgres trusts local connections.

   **Supabase** – find your credentials in the Supabase dashboard under
   *Project Settings → Database*. Use the connection string values for:
   - `SUPABASE_DB_HOST` – looks like `db.xxxxxxxxxxxx.supabase.co`
   - `SUPABASE_DB_USER` – typically `postgres`
   - `SUPABASE_DB_PASSWORD` – your project database password

3. Export the variables before running scripts:
   ```bash
   export $(grep -v '^#' baby_data/scripts/.env | xargs)
   ```
   Or use a tool like `python-dotenv` / `direnv` to load them automatically.
