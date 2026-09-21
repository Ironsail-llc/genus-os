-- Approval evidence outlives the current chat draft and worker process.
CREATE TABLE IF NOT EXISTS chat_approval_receipts (
    tenant_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_state JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, request_id),
    UNIQUE (tenant_id, session_key, plan_id)
);
ALTER TABLE chat_approval_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_approval_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON chat_approval_receipts;
CREATE POLICY tenant_isolation ON chat_approval_receipts USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
INSERT INTO chat_approval_receipts(tenant_id,request_id,session_key,plan_id,plan_state)
SELECT tenant_id,plan_state->>'approval_request_id',session_key,plan_state->>'plan_id',plan_state
FROM chat_sessions WHERE plan_state->>'status'='approved'
  AND COALESCE(plan_state->>'approval_request_id','')<>''
  AND COALESCE(plan_state->>'plan_id','')<>''
ON CONFLICT DO NOTHING;
