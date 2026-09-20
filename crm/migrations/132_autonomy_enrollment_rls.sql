-- Forward repair for enrollment tenant isolation; preserve migration 131 checksums.
-- Reapply the established backstop only to tables missing tenant_isolation.
-- Existing policies remain unchanged; scoped application checks remain required.

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
