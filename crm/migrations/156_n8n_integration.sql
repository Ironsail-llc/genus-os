-- Migration 156: the n8n monitor's snapshot (robothor/integrations/n8n/store.py).
--
-- n8n_workflows: the external n8n instance's workflows as of the latest
-- successful listing, one row per (tenant, workflow). A workflow deleted in
-- n8n is deleted here on the next listing. tags is a JSON array of names.
--
-- n8n_executions: failed executions the poller has seen. alerted_at is set
-- once the main agent has been notified, which makes the notification
-- once-only across rounds, restarts and leader changes. error_message is the
-- error n8n recorded, read for new executions only. Rows older than 30 days
-- are purged by the poller.
--
-- n8n_sync_state: per tenant, when the poller last ran (last_poll_at), when
-- it last succeeded (last_ok_at; the doctor's poll-freshness check reads
-- it), the last failure's error class and the workflow totals.
CREATE TABLE IF NOT EXISTS n8n_workflows (
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT false,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    remote_updated_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workflow_id)
);

CREATE TABLE IF NOT EXISTS n8n_executions (
    tenant_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'error',
    mode TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    error_message TEXT,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    alerted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, execution_id)
);

CREATE INDEX IF NOT EXISTS n8n_executions_workflow_started
    ON n8n_executions (tenant_id, workflow_id, started_at DESC);

CREATE TABLE IF NOT EXISTS n8n_sync_state (
    tenant_id TEXT PRIMARY KEY,
    last_poll_at TIMESTAMPTZ,
    last_ok_at TIMESTAMPTZ,
    last_error TEXT,
    workflows_total INTEGER NOT NULL DEFAULT 0,
    workflows_active INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE n8n_workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE n8n_workflows FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON n8n_workflows;
CREATE POLICY tenant_isolation ON n8n_workflows
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE n8n_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE n8n_executions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON n8n_executions;
CREATE POLICY tenant_isolation ON n8n_executions
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE n8n_sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE n8n_sync_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON n8n_sync_state;
CREATE POLICY tenant_isolation ON n8n_sync_state
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS n8n_sync_state;
--   DROP TABLE IF EXISTS n8n_executions;
--   DROP TABLE IF EXISTS n8n_workflows;
