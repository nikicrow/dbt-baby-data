# Supabase migrations

Security / configuration changes applied directly to the hosted Supabase
project (`Baby data prod`, ref `ffbvvcrynewjnafycrnx`) that are **not** part of
the app's Alembic-managed table DDL.

The source tables themselves are owned by the app repo
(`baby-data-app-2025`) via Alembic; this repo only reads them as dbt sources.
Files here cover Supabase-specific concerns (RLS, role grants) layered on top.

## How these are applied

Applied through Supabase's migration history (via the Supabase MCP
`apply_migration`, which records them under `supabase_migrations.schema_migrations`).
Each `.sql` here is the reviewable copy of what was applied. They are written to
be idempotent so re-running is safe.

| File | Applied | What |
|------|---------|------|
| `20260720_enable_rls_and_revoke_anon_grants.sql` | 2026-07-20 | Enable RLS on all 7 public tables and revoke `anon`/`authenticated` grants, locking the auto-generated REST/GraphQL API. `postgres` (app + dbt) and `service_role` are unaffected. |

## Note for a fresh bootstrap

If the Supabase project is ever recreated from scratch, the app's Alembic
`upgrade head` recreates the tables but **not** this RLS/grant state — re-apply
the migration(s) here afterwards.
