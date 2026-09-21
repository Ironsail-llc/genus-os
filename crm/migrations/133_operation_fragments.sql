-- Immutable partial results, admitted only under the parent job's active lease.
CREATE TABLE IF NOT EXISTS operation_fragments (
 tenant_id TEXT NOT NULL,
 job_id UUID NOT NULL REFERENCES operation_jobs(id) ON DELETE CASCADE,
 fragment_key TEXT NOT NULL CHECK(length(fragment_key) BETWEEN 1 AND 100),
 input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 payload JSONB NOT NULL,
 payload_hash TEXT NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,job_id,fragment_key)
);
ALTER TABLE operation_fragments ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_fragments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_fragments;
CREATE POLICY tenant_isolation ON operation_fragments
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));
