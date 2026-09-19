-- Broker observations remain separate from model-readable resources and journals.
CREATE TABLE IF NOT EXISTS autonomy_terms_snapshots (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    operation_id uuid NOT NULL REFERENCES autonomy_operations(id),
    version integer NOT NULL CHECK (version > 0),
    grant_version integer NOT NULL CHECK (grant_version > 0),
    phase text NOT NULL CHECK (phase IN ('before_input','before_submit')),
    coverage text NOT NULL CHECK (coverage = 'visible_text_only'),
    document_count integer NOT NULL CHECK (document_count BETWEEN 1 AND 21),
    encrypted_value bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(operation_id, version)
);
CREATE INDEX IF NOT EXISTS autonomy_terms_owner
    ON autonomy_terms_snapshots(tenant_id,owner_id,operation_id);
ALTER TABLE autonomy_terms_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_terms_snapshots FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_terms_snapshots;
CREATE POLICY tenant_isolation ON autonomy_terms_snapshots
USING (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id))
WITH CHECK (COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id));
