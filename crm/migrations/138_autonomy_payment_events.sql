-- Personal payment facts are encrypted and scoped separately from organizational treasury.
CREATE TABLE IF NOT EXISTS autonomy_payment_events (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    operation_id uuid NOT NULL REFERENCES autonomy_operations(id),
    version integer NOT NULL CHECK (version > 0),
    event_key_digest text NOT NULL,
    encrypted_value bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(operation_id,version),
    UNIQUE(operation_id,event_key_digest)
);
CREATE INDEX IF NOT EXISTS autonomy_payment_owner
    ON autonomy_payment_events(tenant_id,owner_id,operation_id);
ALTER TABLE autonomy_payment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_payment_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_payment_events;
CREATE POLICY tenant_isolation ON autonomy_payment_events
USING (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id))
WITH CHECK (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id));
