-- Migration 154: plugin_state, the one table plugin wheels store data in
-- (robothor/plugins/state.py).
--
-- A plugin wheel cannot ship a migration, so every "table" a plugin needs is a
-- row kind here: (tenant, plugin, kind, key) -> a JSON object. expires_at is
-- NULL for a row that lives until deleted; a row past it is invisible to
-- PluginState reads at once and is deleted by the daemon's hourly purge.
CREATE TABLE IF NOT EXISTS plugin_state (
    tenant_id TEXT NOT NULL,
    plugin TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, plugin, kind, key)
);

CREATE INDEX IF NOT EXISTS plugin_state_kind ON plugin_state (tenant_id, plugin, kind);
CREATE INDEX IF NOT EXISTS plugin_state_expiry ON plugin_state (expires_at)
    WHERE expires_at IS NOT NULL;

ALTER TABLE plugin_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE plugin_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON plugin_state;
CREATE POLICY tenant_isolation ON plugin_state
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS plugin_state;
