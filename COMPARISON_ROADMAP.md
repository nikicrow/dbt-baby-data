# Roadmap: Comparing Ember and Imogen in the Frontend

## Goal

See Imogen's data in the frontend app (`baby-data-app-2025`) and compare her to Ember on the
questions that matter right now:

- How long are Imogen's **night sleeps**, and how do they compare to Ember's at the same age?
- How **often is she feeding**?
- How long are her **naps**?
- How long are her **wake windows**?

Because the girls were born ~2.5 years apart (Ember **2023-08-18**, Imogen **2026-03-13**), every
comparison is **age-aligned** — plotted against age in days/weeks, never calendar dates.

**Priority: local PostgreSQL first.** Supabase stays wired as a second target (`--target supabase`)
but is deferred to the final phase.

## The stack today

| Piece | Repo | What it does |
|---|---|---|
| Pipeline | `dbt-baby-data` | CSV export zips → `transform_seeds.py` (Pydantic `BABIES` config) → `load_to_database.py` → Postgres tables `baby_profiles`, `feeding_sessions`, `sleep_sessions`, `diaper_events`. `ingest.py` orchestrates zip → transform → load. |
| dbt models | `dbt-baby-data` | Raw passthrough views only — currently point at a `baby_app` schema that nothing writes to (see Phase 2). |
| Backend | `baby-data-app-2025/backend` | FastAPI + SQLAlchemy reading the same Postgres tables directly (schema `public`). Already baby-agnostic — everything keyed by `baby_id`. |
| Frontend | `baby-data-app-2025/frontend` | React + Vite + Recharts. Insights dashboard with Sleep/Feeds/Nappies/Growth tabs. Silently picks the *first* baby — no switcher, no comparison view. |

## Things to know before starting

1. **Imogen's DOB is 2026-03-13.** `BABIES` in `baby_data/scripts/transform_seeds.py` currently
   has `2026-03-14` (inferred from her earliest tracked entry). Must be corrected — a wrong DOB
   shifts every age-aligned chart by a day.
2. **Ingest wipes app-logged data.** `load_to_database.py` truncates and re-inserts. Anything
   logged through the frontend's Log Activity form is lost on the next ingest. Treat the CSV
   export (Huckleberry zip) as the single source of truth until upsert loading exists (out of
   scope for now).
3. **Schema mismatch.** dbt sources reference `baby_app.<table>`, but the loader writes to
   `public` locally and FastAPI reads `public`. The existing dbt models are disconnected from the
   real data; Phase 2 fixes the source definitions.
4. **Pump legacy naming.** Ember's mum-pump file is the unprefixed `pump.csv`;
   `transform_seeds.py` (~line 300) already handles this. Imogen has no pump file and the
   transform tolerates that.

---

## Phase 1 — Load the new data into local Postgres

Latest export: `csv (4).zip` (Imogen sleep 790 rows, nursing 887, diaper 861 — roughly 5× the
current seeds; Ember's files refreshed too).

1. Correct Imogen's `date_of_birth` in `BABIES` (`baby_data/scripts/transform_seeds.py`):
   `2026-03-14` → `2026-03-13`.
2. Run the ingest (default target = local):
   ```bash
   python baby_data/scripts/ingest.py --zip "C:\Users\nikil\Downloads\csv (4).zip"
   ```
3. Verify:
   - `baby_profiles` has 2 rows (Ember, Imogen) with correct DOBs.
   - Row counts in `feeding_sessions` / `sleep_sessions` / `diaper_events` match the transformed
     CSVs in `baby_data/transformed_data/`.
   - Open the frontend — Imogen's events exist in the DB (she may not be visible yet; the UI
     shows the first baby only until Phase 4).

## Phase 2 — dbt comparison marts

All the comparison logic lives in dbt (`baby_data/models/`) so the same models run unchanged
against Supabase later.

1. **Fix sources.** Point the `baby_app` source at the schema the loader actually writes
   (`public`), ideally via `env_var()` with a default so the same definition serves Supabase's
   `baby_data` schema later.
2. **Staging models** — `stg_sleep_sessions`, `stg_feeding_sessions`, `stg_diaper_events`:
   join each event table to `baby_profiles` for DOB and add `age_days` / `age_weeks` to every row.
3. **Marts** (materialize as tables in a dedicated schema, e.g. `marts`):
   - `fct_sleep_sessions` — per session: baby, start/end, duration, `is_night` (reuse the
     existing 7pm–7am rule). Note: a night's sleep often spans midnight and splits into multiple
     rows — attribute a "night" to the date it *starts* when aggregating.
   - `fct_wake_windows` — `LEAD()` over sleep sessions per baby: gap from one sleep's `end_time`
     to the next sleep's `start_time`, daytime windows only.
   - `mart_daily_metrics` — **one row per baby per day**, the table that answers every
     comparison question:
     `age_days`, `age_weeks`, `night_sleep_minutes`, `nap_count`, `total_nap_minutes`,
     `avg_nap_minutes`, `feed_count`, `avg_feed_interval_minutes`, `avg_wake_window_minutes`,
     `max_wake_window_minutes`, diaper counts.
4. **Tests + run:** `not_null` / sensible-range tests, then `dbt run && dbt test`.

## Phase 3 — FastAPI read endpoints (`baby-data-app-2025/backend`)

1. New router `app/api/analytics.py`:
   - `GET /api/v1/analytics/daily-metrics?baby_id=…` — one baby's daily metric rows.
   - `GET /api/v1/analytics/compare?align=age_weeks` — both babies' metrics, aligned on age, in
     one response.
   Read from the `marts` schema via SQLAlchemy `Table(..., schema="marts")` or raw SQL.
2. Pydantic response models for the metric rows.

## Phase 4 — React comparison UI (`baby-data-app-2025/frontend`)

1. **Baby switcher** in the app header — `App.tsx` currently auto-picks the first baby; make the
   current baby a user choice that all existing tabs respect.
2. **New "Compare" tab** in the Insights dashboard (`InsightsDashboard.tsx`):
   - Recharts line charts overlaying Ember vs Imogen on an **age-in-weeks x-axis** for: night
     sleep duration, nap totals/averages, wake windows, feeds per day.
   - Summary cards, e.g. *"At 16 weeks: Imogen avg night sleep X h vs Ember Y h"*.
   - Cap the x-axis at Imogen's current age (~16 weeks) — that's the overlap window; it grows as
     she does.

## Phase 5 (deferred) — Supabase

1. Bootstrap per `SUPABASE_MIGRATION_PLAN.md`: fill `SUPABASE_DB_*` env vars, then
   `dbt seed --target supabase && dbt run --target supabase`, then
   `python baby_data/scripts/ingest.py --target supabase`.
2. The Phase 2 marts run there with `--target supabase` — no model changes.
3. Switch the frontend by pointing the FastAPI backend's `POSTGRES_*` env vars at the Supabase
   connection.

## Out of scope (for now)

- **Growth / health comparisons** — `growth_measurements` and `health_events` tables exist in the
  app but the pipeline has no data source for them.
- **Upsert-style loading** to preserve events logged through the app between ingests.

## Definition of done

Open the frontend, flip to the Compare tab, and see Imogen's night sleep, naps, wake windows, and
feeds-per-day plotted against Ember's at the same age — backed by `mart_daily_metrics` in local
Postgres, with the identical models ready to run on Supabase.
