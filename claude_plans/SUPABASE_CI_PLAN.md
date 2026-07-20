# Supabase CI Plan

## Goal

Run `dbt build` against a test schema on every PR into `main`, so failing dbt
tests block the merge. Supabase (`Baby data prod`) becomes the production
database; CI reads its real source tables and builds models into a throwaway
per-PR schema.

## Status

| Phase | What | State |
|-------|------|-------|
| 0 | Bootstrap Supabase: create source tables, load data | **Done (2026-07-19)** |
| 1 | dbt config: CI profile, schema isolation | Done (`71cc888`) |
| 2 | GitHub Actions workflow + repo secrets | **Done** — workflow (`71cc888`), five repo secrets set, CI green on PR |

Phase 0 landed on 2026-07-19 (see [Completed](#completed--2026-07-19) below).
`dbt build` now succeeds against Supabase with real data. All five repo secrets
are set and the CI check is green on a PR into `main`, so Phases 0–2 are
complete. Remaining work is tracked under [Known gaps / follow-ups](#known-gaps--follow-ups)
(RLS, app connection cutover).

## Completed — 2026-07-19

Phase 0 was executed against `Baby data prod` (ref `ffbvvcrynewjnafycrnx`):

1. **Schema created via Alembic.** Ran `alembic upgrade head` from the app repo
   (`baby-data-app-2025/backend`) against the Supabase session pooler. This
   created the six source tables plus `alembic_version`, stamped at head
   `e7a91b4c2d58` — so the app's migration history matches Supabase exactly.
2. **Data loaded.** `load_to_database.py --target supabase` inserted the
   pipeline data: `baby_profiles` 2, `diaper_events` 3182, `sleep_sessions`
   2519, `feeding_sessions` 3008 — 8711 rows total. `growth_measurements` and
   `health_events` are app-only and were left empty.
3. **dbt build verified.** Ran the exact CI command locally against Supabase
   (`dbt build --target ci --profiles-dir ../ci --exclude resource_type:seed`
   with `DBT_SOURCE_DATABASE=postgres`, `DBT_SOURCE_SCHEMA=public`), building
   into a throwaway `ci_local_test` schema: **PASS=43 WARN=0 ERROR=0**. The
   test schemas were dropped afterwards.

### Fixes made along the way (`load_to_database.py`)

- **Percent-encode credentials** in `get_connection_string` — a Supabase
  password containing URL-special characters (`@ : / ?`) would otherwise
  corrupt the connection URI.
- **`extra="ignore"` on the config classes** — the shared `.env` holds both the
  `DB_*` and `SUPABASE_DB_*` blocks, so loading the Supabase config was failing
  with `extra_forbidden` errors on the local `DB_*` keys.

### Connection details (confirmed from the dashboard)

- Pooler host: `aws-1-ap-southeast-2.pooler.supabase.com` — note **`aws-1`**,
  not `aws-0`; confirmed via Connect → **Server** → Session pooler.
- User `postgres.ffbvvcrynewjnafycrnx`, port `5432`, db `postgres`, `sslmode=require`.

## Key facts

- Supabase project ref: `ffbvvcrynewjnafycrnx`, region `ap-southeast-2`.
- As of 2026-07-19 the `public` schema holds the six app tables plus
  `alembic_version`, populated with pipeline data (see Completed above). Before
  that the project contained only Supabase's own system schemas.
- The six source tables are owned by the **app repo** (`baby-data-app-2025`),
  created by its Alembic migrations. This repo only reads them as dbt sources.
- Sources live in the `public` schema on Supabase, matching local. Alembic
  creates them there by default; no `baby_data` schema is involved.
- Local Postgres is at Alembic revision `e7a91b4c2d58`, which is also the head
  the migration files resolve to. Running `alembic upgrade head` against
  Supabase therefore reproduces the exact schema dbt already builds against.

---

## Phase 0 — Bootstrap Supabase (done — 2026-07-19)

> ✅ Completed on 2026-07-19 — see [Completed](#completed--2026-07-19). The steps
> below are retained as the record of how it was done and how to redo it on a
> fresh project. In practice the data load ran `load_to_database.py --target
> supabase` directly (the `transformed_data/` CSVs were already present) rather
> than the full `ingest.py`, and credentials were supplied via `SUPABASE_DB_*`
> environment variables rather than the `.env` file.

### Get the connection details

Supabase dashboard → **Project Settings → Database** (or the **Connect**
button). Use the **Session pooler** block, *not* Direct connection:

- Host: `aws-1-ap-southeast-2.pooler.supabase.com` (confirmed in the dashboard;
  note it is `aws-1`, not `aws-0`)
- User: `postgres.ffbvvcrynewjnafycrnx` — the pooler user, not plain `postgres`
- Port `5432`, database `postgres`, `sslmode=require`

The direct `db.ffbvvcrynewjnafycrnx.supabase.co` host is IPv6-only. It may work
from your laptop but **will not work from GitHub Actions**, whose runners are
IPv4-only. Use the pooler everywhere to avoid two sets of config.

### Step 1 — Inspect the DDL before running it (optional)

Alembic renders every statement offline, without connecting:

```bash
cd ../baby-data-app-2025/backend
DATABASE_URL="postgresql://postgres:x@localhost:5432/postgres" uv run alembic upgrade head --sql
```

The URL is a dummy — offline mode never dials it. Output is ~219 lines: six
`CREATE TABLE`s (`baby_profiles`, `diaper_events`, `feeding_sessions`,
`growth_measurements`, `health_events`, `sleep_sessions`) plus the column
renames and `source` / `updated_at` additions from the four later migrations.

### Step 2 — Create the schema in Supabase

```bash
cd ../baby-data-app-2025/backend
DATABASE_URL="postgresql://postgres.ffbvvcrynewjnafycrnx:<pw>@<pooler-host>:5432/postgres?sslmode=require" \
  uv run alembic upgrade head
```

Verify — should print `e7a91b4c2d58 (head)`:

```bash
DATABASE_URL="postgresql://postgres.ffbvvcrynewjnafycrnx:<pw>@<pooler-host>:5432/postgres?sslmode=require" \
  uv run alembic current
```

### Step 3 — Load the data

Fill in the `SUPABASE_DB_*` block in `baby_data/scripts/.env` (currently all
commented out — nothing has ever connected). Then, from this repo:

```bash
python baby_data/scripts/ingest.py --target supabase
```

`load_to_database.py` only ever deletes rows tagged `source='ingested'`, so
app-created rows are never touched.

### Step 4 — Verify dbt can build against it

```bash
cd baby_data
DBT_SOURCE_DATABASE=postgres dbt debug --target supabase
DBT_SOURCE_DATABASE=postgres dbt build --target supabase
```

---

## Phase 1 — dbt config (done)

- **`ci/profiles.yml`** — CI-only profile, entirely env-var driven, no
  credentials committed. It lives in `ci/` rather than `baby_data/` because dbt
  prefers a `profiles.yml` found in the working directory, so placing it in the
  project dir would silently shadow `~/.dbt/profiles.yml` on local runs.
- **`baby_data/macros/generate_schema_name.sql`** — the isolation fix. The
  macro previously returned any custom schema name *literally*, so `+schema:
  marts` resolved to a schema called exactly `marts` on **every** target — a CI
  run would have overwritten the real marts tables the app reads. The `ci`
  target now keeps dbt's default prefixing; all other targets are unchanged.

  Verified against the compiled manifest:

  | Target | Staging models | Marts models | Sources |
  |--------|----------------|--------------|---------|
  | `ci` | `ci_pr_<n>` | `ci_pr_<n>_marts` | `postgres.public` |
  | `local` | `models` | `marts` (literal) | `baby_data.public` |

- **`baby_data/scripts/load_to_database.py`** — the Supabase loader defaulted to
  a `baby_data` schema; now `public`, matching where Alembic creates the tables.

## Phase 2 — CI workflow (done)

`.github/workflows/dbt-ci.yml` runs on `pull_request` into `main`:

1. `uv sync --frozen`, then `dbt deps`.
2. `dbt build --target ci --profiles-dir ../ci --exclude resource_type:seed`.
   Seeds are excluded because they're raw Baby Tracker exports consumed by
   `scripts/transform_seeds.py` — no model refs them, so building them would
   copy CSVs for nothing.
3. Drops `ci_pr_<n>` and `ci_pr_<n>_marts` in an `if: always()` step, so a
   failed build doesn't leave schemas behind.

A `concurrency` group cancels in-flight runs per PR, so two builds never share
the same schema.

### Required repo secrets

All five are set (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `SUPABASE_DB_HOST` | `aws-1-ap-southeast-2.pooler.supabase.com` (session pooler — **not** `db.<ref>.supabase.co`) |
| `SUPABASE_DB_PORT` | `5432` |
| `SUPABASE_DB_NAME` | `postgres` |
| `SUPABASE_DB_USER` | `postgres.ffbvvcrynewjnafycrnx` |
| `SUPABASE_DB_PASSWORD` | Database password (paste raw — no percent-encoding needed here) |

---

## Sequencing

Opening a PR from a branch containing the workflow triggers it immediately. To
avoid a confusing red check, do Phase 0 and add the secrets **before** opening
the PR. Pushing early is fine if you want to shake out YAML or connection
problems first — just read the failure as a to-do list rather than a defect.

## Known gaps / follow-ups

- **RLS is disabled on all Supabase tables.** Supabase flags this as *critical*:
  the tables are reachable through its auto-generated REST API using the public
  anon key, so anyone with that key could read or modify the data. dbt and the
  app connect as the `postgres` role and bypass RLS, so nothing is currently
  broken — but before this is genuinely prod, enable RLS **with** policies
  (enabling it without policies blocks all API access). See the
  [RLS guide](https://supabase.com/docs/guides/database/postgres/row-level-security).
- **The app still points at local Postgres.** Making Supabase genuinely prod
  means migrating the app's connection too, and deciding what happens to the
  existing local data. Not covered here.
- **`baby_data/scripts/.env.example` still shows the direct-host format**
  (`db.xxxxxxxxxxxx.supabase.co`, user `postgres`). It should show the pooler
  host and `postgres.<ref>` user to match how CI and the loader actually connect.
- **`models/raw/sources.yml` has a stale comment** saying the Supabase source
  schema is `baby_data`; it is `public` (the code already defaults correctly).
- **CI reads prod sources.** A PR build can't corrupt them (dbt only reads
  sources, and writes go to the per-PR schema), but the build is only as stable
  as prod data — a bad ingest can turn PRs red. Seed-based fixtures would
  decouple this if it becomes annoying.
- **`dbt_project.yml` has a stale `models.baby_data.example` config path** with
  no matching directory. Harmless; dbt warns on every run.
- **Local branch `claude/cool-hopper` (`3d80202`) contains a database password**
  in `.claude/settings.local.json`. Never pushed. Delete it rather than push it.
  `.gitignore` now blocks that file, but the branch predates the fix.
