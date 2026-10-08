-- Migration 149: workspace ingest state (robothor/workspace/ingest/state.py).
--
-- workspace_sync_state: one row per (tenant, provider, mailbox, resource) --
-- for Microsoft 365, the assistant inbox ('mail') and the owner calendar
-- ('calendar'). delta_link is the provider's delta position (empty = start a
-- new delta). high_water is the newest item time the ingestor has processed:
-- after a lost delta (Graph 410 syncStateNotFound) only items newer than it
-- are published, so a resync never replays old mail. initialized_at is when
-- the current delta started (a calendar window is renewed daily). updated_at
-- is the last completed round; the doctor's ingest-freshness check reads it.
--
-- workspace_seen: every external id the ingestor has handled, so an item is
-- published once even across a resync. Rows older than the worker's TTL
-- (45 days) are purged by the worker.
CREATE TABLE IF NOT EXISTS workspace_sync_state (
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    resource TEXT NOT NULL,
    delta_link TEXT NOT NULL DEFAULT '',
    high_water TIMESTAMPTZ,
    initialized_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider, mailbox, resource)
);

CREATE TABLE IF NOT EXISTS workspace_seen (
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider, external_id)
);

CREATE INDEX IF NOT EXISTS workspace_seen_age ON workspace_seen (tenant_id, seen_at);

ALTER TABLE workspace_sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_sync_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspace_sync_state;
CREATE POLICY tenant_isolation ON workspace_sync_state
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE workspace_seen ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_seen FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspace_seen;
CREATE POLICY tenant_isolation ON workspace_seen
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS workspace_seen;
--   DROP TABLE IF EXISTS workspace_sync_state;
