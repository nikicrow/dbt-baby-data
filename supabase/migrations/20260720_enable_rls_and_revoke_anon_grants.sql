-- Enable Row Level Security and revoke anon/authenticated grants
--
-- Context: the 7 public tables were created by the app repo's Alembic
-- migrations (baby-data-app-2025). Supabase's defaults left the `anon` and
-- `authenticated` roles with FULL CRUD (SELECT/INSERT/UPDATE/DELETE/TRUNCATE/
-- REFERENCES/TRIGGER) on every table, all reachable via the auto-generated
-- REST + GraphQL API with the public anon key.
--
-- Nothing legitimate uses those roles:
--   * the app (baby-data-app-2025) connects via SQLAlchemy as `postgres`
--   * dbt / CI connect via dbt-postgres as `postgres`
-- The `postgres` role BYPASSES RLS and retains all grants, so this migration
-- does not affect the app or dbt. `service_role` (the backend service key) is
-- also left untouched.
--
-- Defense-in-depth: enabling RLS with no policies blocks all row access for
-- anon/authenticated; revoking the grants additionally removes the objects
-- from the auto-generated API surface (clears advisors 0013, 0023, 0026, 0027).
--
-- Idempotent: ENABLE ROW LEVEL SECURITY and REVOKE are both safe to re-run.

alter table public.baby_profiles       enable row level security;
alter table public.diaper_events        enable row level security;
alter table public.feeding_sessions     enable row level security;
alter table public.growth_measurements  enable row level security;
alter table public.health_events        enable row level security;
alter table public.sleep_sessions       enable row level security;
alter table public.alembic_version      enable row level security;

revoke all on public.baby_profiles      from anon, authenticated;
revoke all on public.diaper_events       from anon, authenticated;
revoke all on public.feeding_sessions    from anon, authenticated;
revoke all on public.growth_measurements from anon, authenticated;
revoke all on public.health_events       from anon, authenticated;
revoke all on public.sleep_sessions      from anon, authenticated;
revoke all on public.alembic_version     from anon, authenticated;
