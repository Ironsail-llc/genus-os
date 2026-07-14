-- RLS hid the global RBAC defaults, and RBAC then denied everything.
--
-- role_permissions is not tenant DATA, it is tenant CONFIG: its rows are tagged
-- tenant_id = '__default__' and are the built-in rules every tenant inherits
-- (viewer/owner/admin/user/service). Migration 081's tenant_isolation policy only
-- admits rows whose tenant_id equals the connection's app.tenant_id, so the moment
-- the engine started connecting as a scoped non-superuser it saw ZERO rules —
-- and RBAC, correctly denying by default, blocked search_memory / read_file /
-- memory_block_read across the whole fleet. The runs still reported "completed",
-- so the only outward sign was a guardrail-event count.
--
-- The fix is to say what was always meant: a tenant sees its own rows PLUS the
-- global defaults. Scoped to role_permissions, which is the only RLS table that
-- carries '__default__' rows.
--
-- A tenant still cannot see another TENANT's overrides, so isolation is intact.

DROP POLICY IF EXISTS tenant_isolation ON role_permissions;

CREATE POLICY tenant_isolation ON role_permissions
    USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
        OR tenant_id = '__default__'
    )
    WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    );
