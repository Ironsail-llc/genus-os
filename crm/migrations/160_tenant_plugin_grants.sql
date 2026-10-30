-- Migration 160: tenant_plugin_grants, the root tenant's per-child plugin
-- decisions (robothor/plugins/grants.py).
--
-- A root operator forces one plugin 'on' or 'off' for one child tenant. The
-- child's engine reads its own rows once per plugin generation: 'off' is
-- refused before import, 'on' loads even when the child's lockfile row says
-- disabled. No row means the child decides for itself.
--
-- A table of its own, not a key in crm_tenants.settings: a child admin may
-- PATCH its tenant's settings, and a save replaces the whole object.
CREATE TABLE IF NOT EXISTS tenant_plugin_grants (
    tenant_id TEXT NOT NULL,
    plugin TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('on', 'off')),
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, plugin)
);

ALTER TABLE tenant_plugin_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_plugin_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_plugin_grants;
CREATE POLICY tenant_isolation ON tenant_plugin_grants
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Grants are the ROOT's authority. tenant_isolation alone would let a
-- connection scoped to the child insert, change or delete its own rows. So:
--   * a permissive policy lets a connection scoped to the row's root tenant
--     read and write it (tenant_isolation only matches the row's own tenant);
--   * restrictive policies require, for every INSERT, UPDATE and DELETE, an
--     unbound connection (the migration runner, an admin shell) or one scoped
--     to the row's root. The child keeps SELECT through tenant_isolation.
-- The root is found by walking crm_tenants.parent_tenant_id; a tenant with no
-- row has no root, so only an unbound connection may write its grants.
CREATE OR REPLACE FUNCTION tenant_plugin_grants_root(t TEXT) RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    WITH RECURSIVE up(id, parent, depth) AS (
        SELECT id, parent_tenant_id, 0 FROM crm_tenants WHERE id = t
        UNION ALL
        SELECT c.id, c.parent_tenant_id, up.depth + 1
        FROM crm_tenants c JOIN up ON c.id = up.parent
        WHERE up.depth < 16
    )
    SELECT id FROM up WHERE parent IS NULL ORDER BY depth DESC LIMIT 1
$$;

DROP POLICY IF EXISTS grants_root_access ON tenant_plugin_grants;
CREATE POLICY grants_root_access ON tenant_plugin_grants
    USING (current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id))
    WITH CHECK (current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id));

DROP POLICY IF EXISTS grants_insert_by_root ON tenant_plugin_grants;
CREATE POLICY grants_insert_by_root ON tenant_plugin_grants AS RESTRICTIVE FOR INSERT
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id));

DROP POLICY IF EXISTS grants_update_by_root ON tenant_plugin_grants;
CREATE POLICY grants_update_by_root ON tenant_plugin_grants AS RESTRICTIVE FOR UPDATE
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id));

DROP POLICY IF EXISTS grants_delete_by_root ON tenant_plugin_grants;
CREATE POLICY grants_delete_by_root ON tenant_plugin_grants AS RESTRICTIVE FOR DELETE
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR current_setting('app.tenant_id', true) = tenant_plugin_grants_root(tenant_id));

-- Rollback:
--   DROP TABLE IF EXISTS tenant_plugin_grants;
--   DROP FUNCTION IF EXISTS tenant_plugin_grants_root(TEXT);
