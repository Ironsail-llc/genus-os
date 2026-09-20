-- Additive runtime identity and durable stop authority. Business receipts stay in their stores.
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS runtime_context JSONB NOT NULL
    DEFAULT '{"runtime_id":"current","runtime_version":"1","checkpoint_version":1}'::jsonb;
CREATE TABLE IF NOT EXISTS agent_runtime_controls (
    tenant_id TEXT NOT NULL,
    run_id UUID NOT NULL REFERENCES agent_runs(id),
    version BIGINT NOT NULL DEFAULT 1,
    action TEXT NOT NULL CHECK (action IN ('pause', 'cancel')),
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
ALTER TABLE agent_runtime_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime_controls FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON agent_runtime_controls;
CREATE POLICY tenant_isolation ON agent_runtime_controls
    USING (current_setting('app.tenant_id',true) IS NULL OR current_setting('app.tenant_id',true)=''
           OR tenant_id=current_setting('app.tenant_id',true))
    WITH CHECK (current_setting('app.tenant_id',true) IS NULL OR current_setting('app.tenant_id',true)=''
           OR tenant_id=current_setting('app.tenant_id',true));
