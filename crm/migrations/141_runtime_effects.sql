-- Write-ahead runtime effect records. Provider evidence, not elapsed time, clears uncertainty.
CREATE TABLE IF NOT EXISTS agent_runtime_effects (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    goal_id TEXT,
    budget_id TEXT,
    tool_name TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','dispatching','uncertain','finished','confirmed','not_applied')),
    version BIGINT NOT NULL DEFAULT 1,
    resolution JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runtime_effects_unresolved_intent
    ON agent_runtime_effects(tenant_id,principal_id,fingerprint)
    WHERE state IN ('prepared','dispatching','uncertain');
CREATE INDEX IF NOT EXISTS agent_runtime_effects_request
    ON agent_runtime_effects(tenant_id,principal_id,request_id);
ALTER TABLE agent_runtime_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime_effects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON agent_runtime_effects;
CREATE POLICY tenant_isolation ON agent_runtime_effects
    USING (current_setting('app.tenant_id',true) IS NULL OR current_setting('app.tenant_id',true)=''
           OR tenant_id=current_setting('app.tenant_id',true))
    WITH CHECK (current_setting('app.tenant_id',true) IS NULL OR current_setting('app.tenant_id',true)=''
           OR tenant_id=current_setting('app.tenant_id',true));
