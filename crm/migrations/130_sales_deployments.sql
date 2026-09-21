-- Settings generations fence prepared deployments, including A -> B -> A edits.
ALTER TABLE sales_settings ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0 CHECK(revision>=0);
CREATE TABLE IF NOT EXISTS sales_deployments (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('deploy','rollback')),
 source_transition_id UUID,
 source_release_id TEXT, target_release_id TEXT,
 base_revision BIGINT NOT NULL CHECK(base_revision>=0),
 previous_config JSONB NOT NULL, target_config JSONB NOT NULL,
 source_artifact JSONB NOT NULL, target_artifact JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'preparing' CHECK(status IN ('preparing','committed','aborted')),
 runtime_evidence JSONB,
 actor TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 CHECK(source_release_id IS NULL OR source_release_id ~ '^[0-9a-f]{64}$'),
 CHECK(target_release_id IS NULL OR target_release_id ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX IF NOT EXISTS sales_deployment_one_pending
 ON sales_deployments(tenant_id) WHERE status='preparing';
ALTER TABLE sales_deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_deployments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_deployments;
CREATE POLICY tenant_isolation ON sales_deployments
 USING (tenant_id=current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
