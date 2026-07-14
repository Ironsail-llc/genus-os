-- The role that RLS can actually constrain.
--
-- Migration 081 enables RLS on every tenant-scoped table. It protects nothing
-- while the application connects as a SUPERUSER: Postgres superusers bypass RLS
-- unconditionally, whatever ENABLE/FORCE say. The engine currently connects as
-- `philip`, which IS a superuser — so RLS as-shipped would have been theatre,
-- the same failure this hardening pass kept finding elsewhere.
--
-- This creates the non-superuser role the app should connect as. Verified on a
-- scratch database: as `robothor_app`, a session scoped to tenant 'acme-corp'
-- sees only Acme's rows; the other tenants' rows are invisible, not merely
-- unqueried. As a superuser, all rows remain visible — which is the point.
--
-- The role is created but NOT adopted here: switching the engine's connection
-- user is an operational step (see docs/runbooks/TENANT_RLS.md), and doing it
-- inside a schema migration would risk locking the instance out of its own
-- database.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'robothor_app') THEN
        CREATE ROLE robothor_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO robothor_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO robothor_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO robothor_app;

-- Future tables/sequences inherit the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO robothor_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO robothor_app;
