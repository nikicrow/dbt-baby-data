# App Connection Cutover to Supabase — Scoping Plan

## Goal

Make Supabase the **real production database** for the app
(`baby-data-app-2025`), not just for the dbt pipeline. Today the FastAPI
backend still reads and writes a **local** PostgreSQL instance; this plan cuts
it over to the Supabase project `Baby data prod`
(ref `ffbvvcrynewjnafycrnx`, `ap-southeast-2`) that dbt + CI already use.

**End-state vision (what we're building toward):**
- Backend + frontend **deployed to Vercel**, reading/writing Supabase.
- A **repeatable CSV → Supabase ingestion pipeline** for the periodic data
  refreshes (export the latest Baby Tracker CSVs, load them, rebuild marts).
- No app data depends on the local Postgres instance anymore.

**Definition of done (this plan):** the backend reads/writes Supabase, analytics
endpoints return dbt-built marts from Supabase, the recurring ingest is a single
trustworthy command, and the Vercel deployment path is de-risked (even if the
actual deploy lands as a follow-up).

---

## Current state (verified 2026-07-20)

### How the app connects
- `backend/app/core/config.py` — `Settings.DATABASE_URL` is either taken
  verbatim if provided as a string, otherwise **assembled** from
  `POSTGRES_SERVER/USER/PASSWORD/DB` as `postgresql://user:pass@server/db` —
  note the assembled form has **no port and no `sslmode`**.
- `backend/app/core/database.py` — a single long-lived SQLAlchemy engine
  (`create_engine(..., pool_pre_ping=True)`), classic `sessionmaker`. Direct
  Postgres, connects as the DB user (`postgres`), which **bypasses RLS** — so
  the RLS work from PR #11 does not block it.
- `backend/migrations/env.py` — Alembic also reads `settings.DATABASE_URL`, so
  whatever the app points at, Alembic migrates too.

### What the app actually reads
- **Source tables** in `public.*` (SQLAlchemy models): `baby_profiles`,
  `diaper_events`, `feeding_sessions`, `sleep_sessions`, `growth_measurements`,
  `health_events`.
- **dbt marts** via raw SQL: `backend/app/services/analytics_service.py` queries
  `marts.mart_daily_metrics`. If that table is missing the endpoints return a
  503 telling the user to run `dbt run`.

### The ingestion tooling that already exists (this repo)
- `baby_data/scripts/transform_seeds.py` — transforms raw Baby Tracker CSV
  exports into the shape dbt/loader expect.
- `baby_data/scripts/ingest.py` — orchestrates transform + load, has a
  `--target {local,supabase}` flag.
- `baby_data/scripts/load_to_database.py` — loads into the DB and **only ever
  deletes/replaces rows tagged `source='ingested'`**, so app-created rows are
  never clobbered. This idempotency property is the backbone of a safe recurring
  ingest.

### What's already in Supabase
- `public.*` bootstrapped with **ingested** pipeline rows (8,711:
  `baby_profiles` 2, `diaper_events` 3182, `sleep_sessions` 2519,
  `feeding_sessions` 3008). `growth_measurements` + `health_events` are **empty**.
- RLS enabled + `anon`/`authenticated` grants revoked (PR #11).
- **No `marts` schema populated in prod** — dbt has only ever built marts into
  throwaway CI schemas (`ci_pr_<n>_marts`), never the literal `marts` schema.

### Deployment
- **There is no deployment yet.** Backend (FastAPI) and frontend (Vite/React)
  run locally; no Dockerfile, no host config, no app CI. Target host is
  **Vercel** for both (see Phase 5).

---

## Key decisions (resolved 2026-07-23, via PR #13 review)

### Decision 1 — Sequencing of the Vercel deploy — **DECIDED: 1a**
Vercel (frontend + backend) is the stated end goal, but it doesn't have to be
step one. Two orderings:
- **1a. Cut the local backend over to Supabase first**, prove the app works
  end-to-end against prod data, *then* deploy to Vercel. Lower risk — separates
  "does the app work on Supabase" from "does it work on serverless".
- **1b. Go straight to Vercel** on Supabase. Fewer intermediate states but
  debugging DB + serverless + deploy at once.

> **Decided: 1a.** Cut the local backend over to Supabase first, prove it works,
> then deploy to Vercel as a follow-up. Phase 3 keeps the connection layer
> serverless-ready so the Vercel move is config, not a rewrite.

### Decision 2 — What happens to existing local data? — **DECIDED: 2b**
Supabase has the **ingested** rows but not app-created rows that live only in
local Postgres (notably `growth_measurements` / `health_events`, and any rows
the app added or edited). Options:
- **2a. Migrate local → Supabase** for the app-owned rows before cutover.
  Preserves everything.
- **2b. Treat Supabase's current state as the new baseline** and abandon
  local-only rows. Only safe if local has nothing worth keeping.

> **Decided: 2b.** The local data was all test data — nothing worth keeping.
> Supabase's current state is the baseline, so **Phase 0 (audit) and Phase 2
> (data migration) are skipped.**

### Decision 3 — Which connection role?
Cut over as `postgres` (matches dbt/app today, bypasses RLS) — simplest — or
create a dedicated least-privilege app role. Given RLS has no policies and the
app relies on bypassing it, **stay on `postgres` for now**; a scoped role is a
security follow-up, not part of the cutover.

---

## The Supabase pooler choice (this drives Phase 3 and Phase 5)

Supabase exposes the DB three ways, and **Vercel changes the right answer**:

| Connection | Port | Best for | Notes |
|-----------|------|----------|-------|
| Direct (`db.<ref>.supabase.co`) | 5432 | nothing here | IPv6-only — won't work from Vercel or CI |
| Session pooler | 5432 | a **persistent** server (local backend, a long-running host) | full session features, prepared statements OK |
| Transaction pooler | 6543 | **serverless / Vercel** functions | ephemeral conns; **must** use `NullPool` and avoid server-side prepared statements / session state |

Because the backend is heading to **Vercel serverless** (many short-lived
invocations that can't hold a pool), the connection layer should be
**environment-driven**: same code, pooler host/port + SQLAlchemy pool class
chosen by env var. Local persistent run → session pooler (5432) + normal pool;
Vercel → transaction pooler (6543) + `NullPool`. Building this in Phase 3 means
Phase 5 doesn't require touching `database.py`.

---

## Dependencies & ordering

The app's analytics **depend on dbt marts existing in Supabase**, and the
recurring ingest is what keeps both source data and marts fresh:

Phases 0 and 2 are skipped (Decision 2b — local data was all test).

```
Phase 1 (marts in Supabase) --> Phase 3 (repoint app, serverless-ready)
                                                       |
                        +------------------------------+------------------------------+
                        v                                                              v
       Phase 4 (verify local against Supabase)                        Phase 6 (recurring ingest loop)
                        |                                                  (ongoing, runs after each export)
                        v
       Phase 5 (deploy FE + BE to Vercel)
```

---

## Proposed phased plan

### Phase 0 — Audit local data — **SKIPPED (Decision 2b)**
Local data was all test data; Supabase's current state is the baseline. No audit
needed.

### Phase 1 — Build marts into Supabase (prod dbt run)
- Run `dbt run --target supabase` (with `DBT_SOURCE_DATABASE=postgres`,
  `DBT_SOURCE_SCHEMA=public`) so marts materialize into the **literal `marts`
  schema** (the `generate_schema_name` macro guarantees this for non-`ci`
  targets) — exactly where `analytics_service.py` looks.
- Confirm `marts.mart_daily_metrics` exists and is populated.

### Phase 2 — Migrate app-owned data — **SKIPPED (Decision 2b)**
No local data to migrate.

### Phase 3 — Repoint the app (serverless-ready)
- Make the connection **env-driven** in `config.py` / `database.py`:
  - Full `DATABASE_URL` from env (do **not** rely on the assembled form — it
    omits `port`/`sslmode`).
  - Pool class chosen by env (e.g. `DB_POOL_MODE=session|transaction`):
    `transaction` → `NullPool` for Vercel; `session` → default pool for local.
  - Keep `pool_pre_ping` for the persistent case.
- Local `.env` for the first cutover (session pooler):
  ```
  DATABASE_URL=postgresql://postgres.ffbvvcrynewjnafycrnx:<pw>@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
  DB_POOL_MODE=session
  ```
- Alembic now targets Supabase too; confirm `alembic current` == `e7a91b4c2d58
  (head)` so no migration runs unexpectedly (preview with `--sql`).
- Update `backend/.env.example` to show the Supabase pooler form + `DB_POOL_MODE`.

### Phase 4 — Verify end-to-end (local against Supabase)
- Start the backend against Supabase (`HOW_TO_RUN.md`), exercise CRUD endpoints
  (create/read a feeding, sleep, diaper) and confirm writes land in Supabase.
- Hit the analytics endpoints and confirm `marts.mart_daily_metrics` returns
  data (no 503).
- Sanity-check row counts and that the frontend renders.

### Phase 5 — Deploy to Vercel (frontend + backend)
- **Frontend** (Vite/React): straightforward Vercel static build. Set the API
  base URL to the deployed backend; build from `frontend/`.
- **Backend** (FastAPI): Vercel Python serverless functions (ASGI entrypoint
  under `api/`, or `vercel.json` build config). Set env vars in Vercel:
  `DATABASE_URL` (transaction pooler, **port 6543**), `DB_POOL_MODE=transaction`,
  `SECRET_KEY`, `BACKEND_CORS_ORIGINS` (the frontend's Vercel domain).
- **Monorepo:** `baby-data-app-2025` has `frontend/` + `backend/`; likely two
  Vercel projects (or a monorepo config) with different root dirs.
- **CORS:** add the frontend Vercel domain to `BACKEND_CORS_ORIGINS`.
- **Watch-outs:** serverless cold starts, function execution-time limits, and
  connection storms — the transaction pooler + `NullPool` from Phase 3 is what
  makes this safe. If serverless FastAPI proves painful, a persistent host
  (Render/Fly/Railway) for the backend with only the frontend on Vercel is the
  fallback.

### Phase 6 — Recurring CSV → Supabase ingestion pipeline (ongoing)
The periodic refresh: you export the latest Baby Tracker CSVs and we load them.
Make it **one trustworthy command**, safe to re-run:
- **Single entrypoint:** `ingest.py --target supabase` should do transform →
  load → **then trigger `dbt run --target supabase`** so marts refresh in the
  same step (otherwise the app 503s / shows stale metrics). Today marts refresh
  is a separate manual step — wire it in.
- **Idempotent + safe:** rely on the existing `source='ingested'` delete/replace
  so re-running the same export doesn't duplicate rows and never touches
  app-created data. Verify this holds for a full re-export.
- **Defined drop location + parsing:** standardize where the exported
  zip/CSV goes, unzip, and honor day-first date parsing and per-baby handling
  (per the Imogen export notes). Document the exact steps.
- **Validation:** print/compare row counts before/after and basic sanity checks;
  fail loudly on anomalies.
- **Cadence:** this pipeline *is* the marts-refresh cadence — every ingest
  rebuilds marts. It stays a **manual local command** (the CSV export is manual)
  unless we later automate.
- **Docs:** a short runbook ("when new data arrives, do X") so future-you can
  run it without re-deriving this.

---

## Risks & mitigations
- **Analytics 503 after cutover** — marts not in Supabase. → Phase 1 prerequisite;
  Phase 6 keeps them fresh.
- **Silent data loss** — cutover abandons local-only rows. → Phase 0 audit gates
  Decision 2.
- **Serverless connection storms on Vercel** — many ephemeral functions exhaust
  DB connections. → transaction pooler (6543) + `NullPool`, wired in Phase 3.
- **Pooler/prepared-statement mismatch** — session-mode assumptions break under
  the transaction pooler. → env-driven pool mode; avoid session-level state.
- **Duplicate rows on re-ingest** — a re-run doubling data. → the
  `source='ingested'` replace semantics; validate with a full re-export test.
- **SSL / wrong host** — assembled URL lacks `sslmode`; direct host is IPv6-only.
  → full pooler `DATABASE_URL` with `sslmode=require`.
- **Accidental Alembic migration** — pointing Alembic at prod could run
  migrations. → verify `alembic current` == head first; preview with `--sql`.

---

## Open questions (for you)
- ~~Decision 1 sequencing~~ — **resolved: 1a** (local-against-Supabase first).
- ~~Decision 2 local data~~ — **resolved: 2b** (Supabase is the baseline).
1. Vercel: one monorepo project or two (frontend + backend)? Custom domain?
2. Ingest cadence: purely manual after each export, or eventually scheduled?
3. Keep connecting as `postgres`, or introduce a scoped app role later?
4. Supabase DB password for the Phase 1 dbt run — how do you want to provide it
   (export into the shell for me to drive, or you run the one command)?

## Out of scope (separate follow-ups)
- A least-privilege app DB role / RLS policies — security follow-up.
- Retiring or repurposing the local Postgres instance once cutover is proven.
- Automating the CSV export itself (it's a manual download today).
