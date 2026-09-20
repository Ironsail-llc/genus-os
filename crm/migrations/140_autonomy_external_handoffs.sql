-- Private device-verification handoffs retain the underlying operation's uncertainty.
CREATE TABLE IF NOT EXISTS autonomy_handoffs (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    operation_id uuid NOT NULL REFERENCES autonomy_operations(id),
    agent_id text NOT NULL,
    request_id uuid NOT NULL,
    fingerprint text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('sms','push','passkey','biometric','issuer','captcha')),
    state text NOT NULL CHECK (state IN ('awaiting_external_action','checking','resolved','expired')),
    encrypted_value bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,owner_id,request_id)
);
CREATE INDEX IF NOT EXISTS autonomy_handoff_owner ON autonomy_handoffs(tenant_id,owner_id,operation_id);
CREATE UNIQUE INDEX IF NOT EXISTS autonomy_handoff_pending ON autonomy_handoffs(operation_id)
    WHERE state IN ('awaiting_external_action','checking');
ALTER TABLE autonomy_handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_handoffs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_handoffs;
CREATE POLICY tenant_isolation ON autonomy_handoffs
USING (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id))
WITH CHECK (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id));
