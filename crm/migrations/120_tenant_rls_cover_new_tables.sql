-- 120_tenant_rls_cover_new_tables.sql
-- Re-apply the tenant_isolation policy to every tenant_id table that exists NOW.
--
-- Migration 081 enumerated the tenant-scoped tables once, at the moment it ran.
-- Every tenant_id table created after that point — 085 sso_binding_grants,
-- 086 user_permissions, 089 face_identities, tables the application creates
-- on its own, and on one production box 071 user_accounts (its migration had
-- not applied when 081 ran) — carried no policy at all. The isolation
-- guarantee was therefore partial, and the uncovered set included the identity
-- tables. Found 2026-09-12 by the doctor review; six tables were bare.
--
-- This is the same loop as 081, deliberately: idempotent, permissive when
-- app.tenant_id is unset, FORCE so the owning role is bound too. It is a
-- point-in-time repair. Two guards keep the gap from reopening:
--   * tests/test_tenant_rls_coverage.py refuses a later migration that creates
--     a tenant_id table without applying tenant_isolation inline;
--   * the doctor check db.rls_coverage reads pg_policies on the live database
--     and repairs with --fix by executing this file.

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT tablename
        FROM pg_tables pt
        WHERE schemaname = 'public'
          AND EXISTS (
              SELECT 1 FROM information_schema.columns c
              WHERE c.table_schema = 'public'
                AND c.table_name = pt.tablename
                AND c.column_name = 'tenant_id'
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_policies p
              WHERE p.schemaname = 'public'
                AND p.tablename = pt.tablename
                AND p.policyname = 'tenant_isolation'
          )
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON public.%I
            USING (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)
            )
            WITH CHECK (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)
            )
        $f$, t);
    END LOOP;
END $$;
