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

-- Rollback:
--   DROP TABLE IF EXISTS tenant_plugin_grants;
