# Deploying the Baby App on Tailscale, on Local Postgres

## Context

The previous direction ([APP_CUTOVER_PLAN.md](APP_CUTOVER_PLAN.md)) was to move the
app onto Supabase and deploy frontend + backend to Vercel. Tailscale makes a
cloud host unnecessary: the app can run on the laptop and be reachable from the
phone over the tailnet. That removes Supabase, Vercel, connection poolers,
`NullPool`, and CORS from the picture entirely.

**Tailnet correction (2026-08-25).** This plan was first written against a
personal tailnet, `tail94d867.ts.net`, that turned out to be the wrong one — it
was created by accident instead of joining the existing family tailnet. The
laptop has since moved to `bennycrow91@gmail.com`'s tailnet, and every hostname
below reflects that:

| | old (wrong) | current |
|---|---|---|
| tailnet | `tail94d867.ts.net` | `tail53f4fd.ts.net` |
| laptop | `unagi`, `100.121.167.36` | `unagi`, `100.79.143.69` |
| app URL | `https://unagi.tail94d867.ts.net` | `https://unagi.tail53f4fd.ts.net` |

The laptop kept the name `unagi` — no collision on the new tailnet. **The phone
has not moved yet:** `pixel-8-pro` is still on the old tailnet, so it cannot
reach the app until it logs in to the new one. The old profile (`fe8f`) still
exists locally; `tailscale serve` config is stored per-profile, so switching
back to it would silently leave the proxy unconfigured. Worth deleting once the
new setup is trusted.

**Good news from the investigation: the Supabase cutover was never executed.**
`backend/.env` still sets only `POSTGRES_*` pointing at `localhost`, and there
are zero references to Supabase anywhere in the app repo. The local `baby_data`
database is complete and current with the last ingest:

| | rows |
|---|---|
| `public.baby_profiles` | 2 |
| `public.diaper_events` | 3182 |
| `public.feeding_sessions` | 3008 |
| `public.sleep_sessions` | 2519 |
| `public.growth_measurements` / `health_events` | 0 |
| `marts.mart_daily_metrics` | 463 |

Alembic is at head `e7a91b4c2d58`. So there is **no database migration to do** —
"switch back to local Postgres" means *don't do the cutover* and delete the
Supabase dependency that remains in dbt CI.

**Outcome:** open `https://unagi.tail53f4fd.ts.net` on the phone, from anywhere,
and log a feed. One process, one URL, laptop-local data.

### Why the API has to be on the tailnet too

The React app is a plain Vite SPA — it executes in the browser **on the phone**,
not on the laptop. `frontend/src/services/api.ts:23` resolves to
`http://localhost:8000`, which on the phone is the phone. So both the static
bundle and the API must be reachable across the tailnet. Since both are exposed
anyway, serving them from **one origin** is strictly less work: no CORS list, no
`VITE_API_URL` baked to a hostname, one process for Task Scheduler.

### Decisions

1. **One origin.** FastAPI serves the built SPA and the API. uvicorn binds
   `127.0.0.1` only; `tailscale serve` is the sole exposed surface.
2. **Task Scheduler at boot**, running whether or not you're logged in.
3. **Supabase decommissioned entirely.** dbt CI moves to an ephemeral Postgres
   service container in GitHub Actions.
4. **Backups skipped for now** — recorded as a follow-up (see Risks).

Two repos are involved: this one (`dbt-baby-data`) and the app repo
(`baby-data-app-2025`).

---

## Phase 1 — Serve the SPA from FastAPI (app repo)

Repo: `C:\Users\nikil\baby-data-app-2025`. Working tree is currently dirty
(`backend/.env.example`, `backend/app/core/config.py` CORS-parsing change,
untracked `AGENTS.md`) — commit or stash that first so this work lands clean.

**`backend/app/core/config.py`** — add one setting:
```python
FRONTEND_DIST: Path = Path(__file__).resolve().parents[3] / "frontend" / "build"
```
(`app/core/config.py` → `parents[3]` is the repo root.) Overridable via `.env`.

**`backend/app/main.py`** — currently mounts no static files at all. Add, *after*
every `include_router` call so API routes always win:

- `app.mount("/assets", StaticFiles(directory=settings.FRONTEND_DIST / "assets"))`
- a catch-all `@app.get("/{full_path:path}", include_in_schema=False)` that
  returns the file if it exists, else `FileResponse(index.html)` — required
  because `main.tsx` uses `BrowserRouter`, so `/insights` and
  `/activityhistory` must survive a refresh. Raise 404 for paths starting
  `api/` so a mistyped endpoint doesn't return HTML.
- Guard the whole block on `settings.FRONTEND_DIST.exists()` and log a warning
  otherwise, so the backend still boots before the first `npm run build`.
- The existing `GET /` banner route must move (to `/api/v1/`) or be deleted —
  `/` now serves the SPA. Keep `GET /health` as-is; it's the Task Scheduler and
  `tailscale serve` smoke check.

**`frontend/src/services/api.ts:23`** — change `||` to `??` so an empty value
means same-origin:
```ts
const API_BASE_URL = import.meta.env.VITE_API_URL ?? '';
```
Then add two files so dev and prod both work:
- `frontend/.env.development` → `VITE_API_URL=http://localhost:8000` (keeps the
  existing two-terminal `npm run dev` workflow on port 3000 working)
- `frontend/.env.production` → `VITE_API_URL=` (empty → relative URLs)

`vite.config.ts` needs no change: `outDir` is already `build`, and the bundle
references `/assets/...` from the domain root, which is exactly what the mount
above provides.

**`backend/.env`** — no change. It already points at local Postgres.
`BACKEND_CORS_ORIGINS` becomes irrelevant for the phone under one origin; leave
the localhost entries for dev.

## Phase 2 — Refresh local data

Local Imogen data stops at `2026-07-04` and today is `2026-08-24`; the dbt repo
has uncommitted changes to `baby_data/seeds/Imogen_{diaper,nursing,sleep}.csv`,
so a newer export is already staged. In `C:\Users\nikil\dbt-baby-data`:

```bash
python baby_data/scripts/ingest.py
```
It defaults to `--target local`, finds the latest `csv*.zip` in `~/Downloads`,
and only replaces rows tagged `source='ingested'`, so app-created rows are safe.
Then rebuild marts (`~/.dbt/profiles.yml` already defaults to `target: local`):

```bash
cd baby_data && dbt run && dbt test
```

Verify `marts.mart_daily_metrics` advances past `2026-07-04` — the Insights tab
503s without it.

## Phase 3 — Build and run under Tailscale

**HTTPS certificates: already enabled — nothing to do.** This step originally
called for turning on DNS → HTTPS Certificates in the admin console. That was
true of the old tailnet, but the tailnet this laptop now belongs to already has
it on: `tailscale status --json` reports
`CertDomains: ['unagi.tail53f4fd.ts.net']`. The account also holds
`https://tailscale.com/cap/is-admin` there, so no permission is needed from the
tailnet owner either.

Then:
```bash
cd C:/Users/nikil/baby-data-app-2025/frontend && npm run build
```
```bash
cd C:/Users/nikil/baby-data-app-2025/backend && .venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
```bash
tailscale serve --bg 8000
```

`tailscale serve` proxies `https://unagi.tail53f4fd.ts.net` → `127.0.0.1:8000`
with a Let's Encrypt cert, and the config persists across reboots in the
tailscaled state — it does not need re-running. Note this replaces the current
`--host 0.0.0.0 --reload` invocation in `HOW_TO_RUN.md`: binding `0.0.0.0` also
exposes a **completely unauthenticated CRUD API, DELETE included**, to whatever
café Wi-Fi you're on. `127.0.0.1` + `tailscale serve` means only devices on the
tailnet can reach it. `serve` may prompt for elevation on Windows.

## Phase 4 — Start at boot

Add `scripts/start-app.ps1` to the app repo: `cd` to `backend/`, exec
`.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
(no `--reload`).

Register a Scheduled Task `BabyDataApp`:
- Trigger: **At startup**, delay 1 min (lets `postgresql-x64-15`, already set to
  Automatic, finish starting).
- "Run whether user is logged on or not", highest privileges.
- Action: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <path>\start-app.ps1`
- Settings: restart on failure, every 1 min, up to 3 times.

Register it with `schtasks /create /XML` from a checked-in task XML so it's
reproducible rather than hand-clicked. Power settings are already favourable:
on AC the sleep timeout is 0 (never).

## Phase 5 — Decommission Supabase

**Rework dbt CI** — `.github/workflows/dbt-ci.yml` currently reads real Supabase
source tables and builds into `ci_pr_<n>` schemas. Replace with a self-contained
job:
- `services: postgres: image: postgres:15` on the runner.
- Snapshot the source DDL into `ci/source_schema.sql`, generated once with
  `pg_dump --schema-only` off the local DB for the six `public` tables
  (`baby_profiles`, `diaper_events`, `feeding_sessions`, `sleep_sessions`,
  `growth_measurements`, `health_events`). This is needed because
  `baby_data/scripts/load_to_database.py` only ever `INSERT`s — it calls
  `check_table_exists` and aborts if tables are missing; it never issues DDL.
- Steps: checkout → `uv sync --frozen` → `psql -f ci/source_schema.sql` →
  `python baby_data/scripts/run_pipeline.py` (transforms and loads the committed
  seed CSVs) → `dbt deps` → `dbt build --target ci`.
- Delete the final "Drop CI schemas" step — the container is thrown away.
- `ci/profiles.yml`: rename `SUPABASE_DB_*` to `CI_DB_*` with localhost
  defaults, drop `sslmode: require`. **No repo secrets needed at all** after
  this; delete the five `SUPABASE_DB_*` secrets in GitHub settings.

**Strip Supabase from the dbt repo:**
- `~/.dbt/profiles.yml` — delete the `supabase` output.
- `baby_data/scripts/load_to_database.py` — remove the `SupabaseConfig` class
  and the `supabase` choice from `--target`; likewise `ingest.py`. Leaving it in
  is a footgun pointed at a dead project.
- `baby_data/scripts/.env.example` — delete the stale Supabase block (it still
  documents the IPv6-only direct host).
- `baby_data/models/raw/sources.yml` — fix the stale comment claiming the
  Supabase override is `DBT_SOURCE_SCHEMA=baby_data`.
- `README.md` — rewrite the CI section and drop the `--target supabase`
  instructions and the secrets table.
- Delete `supabase/migrations/` (the RLS hardening SQL) — it only ever applied
  to the project being retired.
- Move `claude_plans/APP_CUTOVER_PLAN.md` to `claude_plans/closed/` with a
  one-line note that Tailscale superseded it.

**Retire the project itself:** *pause* Supabase project `ffbvvcrynewjnafycrnx`
rather than deleting it, and delete it only once CI has been green for a week.
This is a dashboard action for you.

## Phase 6 — Docs

- Rewrite `HOW_TO_RUN.md` in the app repo around the two modes: dev
  (`npm run dev` + uvicorn on :8000) and deployed (build + Task Scheduler +
  `tailscale serve`). It currently names service `postgresql-x64-16`; the
  installed service is **`postgresql-x64-15`**.
- **It also contains the Postgres password in cleartext and is tracked in git.**
  Scrub it as part of this rewrite. Scrubbing won't purge it from history, so
  rotating the `postgres` password is a reasonable follow-up now that the DB is
  laptop-only.
- Commit this plan as `claude_plans/TAILSCALE_DEPLOY_PLAN.md` in the dbt repo,
  alongside the cutover plan it replaces.

---

## Verification

1. `curl http://127.0.0.1:8000/health` → `{"status":"healthy",...}`.
2. `curl https://unagi.tail53f4fd.ts.net/health` from the laptop, then
   `tailscale serve status` shows the proxy. **Caveat:** curl on Windows uses
   schannel, and hitting the node's own tailnet name from that node returns a
   bare `000` after a valid TLS handshake — intermittently, and for every path
   including ones that had just worked. It is a local hairpin artifact, not a
   server fault: the requests never reach uvicorn (nothing appears in its log).
   Confirm with a different TLS stack instead, e.g.
   `.venv/Scripts/python -c "import urllib.request;
   print(urllib.request.urlopen('https://unagi.tail53f4fd.ts.net/health').read())"`,
   or just test from the phone.
3. On the phone: open `https://unagi.tail53f4fd.ts.net` — valid cert, no
   warning. Navigate to Insights and Activity History, then **hard-refresh on
   `/insights`** to prove the SPA fallback works.
4. Log a feed from the phone, then confirm it landed:
   `select * from public.feeding_sessions order by created_at desc limit 1;`
   — it should have `source` distinct from `'ingested'`.
5. Insights renders charts (proves `marts.mart_daily_metrics` is reachable and
   fresh — a 503 means Phase 2 didn't take).
6. `cd backend && .venv/Scripts/python -m pytest` — the 89 existing unit tests
   still pass.
7. **Reboot the laptop**, wait ~2 min, and hit the URL from the phone without
   touching the laptop.
8. Open a throwaway PR in the dbt repo and confirm the reworked CI goes green
   with no secrets configured.

## Risks

- **Laptop asleep = app down.** On battery it sleeps after 45 min; on AC it
  never does. Plugged in, this is fine; unplugged, the phone gets nothing.
- **Local Postgres becomes the only copy of the data.** Backups were deliberately
  deferred; the Baby Tracker CSV exports cover only `source='ingested'` rows, so
  anything logged in the app would be lost with the laptop. A scheduled
  `pg_dump` to OneDrive is the obvious follow-up.
- **CI DDL snapshot drifts** from the app's Alembic migrations. Mitigation: note
  in `ci/source_schema.sql` that it must be regenerated when a migration lands;
  a schema mismatch fails CI loudly rather than silently.
- **`frontend/src/index.css` is precompiled Tailwind v4 with no build step.** Any
  new utility class silently does nothing — this already caused commit
  `55dafde`. Not introduced here, but it constrains any UI tweak.
- **`App.tsx` auto-creates a baby named "Baby"** when it finds zero babies. Never
  point the frontend at an empty database.
- **Postgres listens on `0.0.0.0:5432`.** Worth setting
  `listen_addresses = 'localhost'` in `postgresql.conf` while hardening.

## Out of scope

- Authentication for the app (the tailnet is the perimeter for now).
- Automating the Baby Tracker CSV export itself — still a manual download.
- Wiring `dbt run` into `ingest.py` so marts refresh in one command (carried
  over from the cutover plan's Phase 6; still worth doing).
