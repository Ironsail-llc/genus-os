-- Tenant isolation backstop: Row-Level Security on every tenant-scoped table.
--
-- WHY. Isolation currently rests entirely on the application remembering a
-- WHERE clause. robothor/crm/dal.py does (87 tenant predicates); the bridge's
-- crm_dal.py does NOT (zero) — its read helpers take a bare id and no tenant
-- (`get_company(company_id)`), so the HTTP API can return any tenant's row by
-- id. Rewriting 2,071 lines of DAL is one answer; making the database refuse
-- to serve the row in the first place is the durable one. A missing WHERE
-- clause then leaks nothing.
--
-- HOW IT DEGRADES. The policy is deliberately permissive when
-- `app.tenant_id` is unset: existing connections (migrations, psql, the CLI,
-- anything not yet tenant-aware) keep working exactly as before. Isolation
-- engages for connections that DO set it — which robothor.db.connection does
-- when ROBOTHOR_RLS_ENABLED=1. So this migration is a no-op until the flag is
-- turned on, and reversible by turning it off.
--
-- Note the table owner bypasses RLS unless FORCE is set; FORCE is applied so
-- the engine's own role is subject to the policy too. Without it this whole
-- exercise would be theatre — which is the failure mode this hardening pass
-- kept finding.

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
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
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
