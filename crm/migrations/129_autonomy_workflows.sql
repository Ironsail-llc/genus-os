BEGIN;

CREATE TABLE IF NOT EXISTS autonomy_workflows (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    operation_id UUID NOT NULL UNIQUE REFERENCES autonomy_operations(id),
    instance_id UUID NOT NULL,
    origin TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','completed','closed','lost')),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE autonomy_operations ADD COLUMN IF NOT EXISTS workflow_id UUID REFERENCES autonomy_workflows(id);
CREATE INDEX IF NOT EXISTS autonomy_workflows_owner ON autonomy_workflows(tenant_id,owner_id,agent_id);

CREATE TABLE IF NOT EXISTS autonomy_workflow_commands (
    workflow_id UUID NOT NULL REFERENCES autonomy_workflows(id),
    command_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    request JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running','completed')),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(workflow_id,command_id)
);

ALTER TABLE autonomy_workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_workflows FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_workflows;
CREATE POLICY tenant_isolation ON autonomy_workflows
USING (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id))
WITH CHECK (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id));
ALTER TABLE autonomy_workflow_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_workflow_commands FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_workflow_commands;
CREATE POLICY tenant_isolation ON autonomy_workflow_commands
USING (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id))
WITH CHECK (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id));

COMMIT;
