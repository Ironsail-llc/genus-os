-- Qualification quality assessment is separate from promotion/send authority.
CREATE TABLE IF NOT EXISTS sales_calibration_cohorts (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
 target_size INTEGER NOT NULL CHECK(target_size BETWEEN 1 AND 1000),
 agreement_target_percent INTEGER NOT NULL CHECK(agreement_target_percent BETWEEN 0 AND 100),
 policy_versions JSONB NOT NULL, policies JSONB NOT NULL,
 settings_revision BIGINT NOT NULL, created_by TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS sales_calibration_items (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, cohort_id UUID NOT NULL,
 prospect_id UUID NOT NULL REFERENCES sales_prospects(id), ordinal INTEGER NOT NULL CHECK(ordinal>0),
 buying_case TEXT NOT NULL, policy_version TEXT NOT NULL,
 snapshot JSONB NOT NULL, snapshot_hash TEXT NOT NULL CHECK(snapshot_hash ~ '^[a-f0-9]{64}$'),
 enrolled_by TEXT NOT NULL, enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,cohort_id,prospect_id), UNIQUE(tenant_id,cohort_id,ordinal),
 FOREIGN KEY(tenant_id,cohort_id) REFERENCES sales_calibration_cohorts(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS sales_calibration_assessments (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, item_id UUID NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), supersedes UUID REFERENCES sales_calibration_assessments(id),
 reference_decision TEXT NOT NULL CHECK(reference_decision IN ('qualified','rejected','needs_research')),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,item_id,revision),
 FOREIGN KEY(tenant_id,item_id) REFERENCES sales_calibration_items(tenant_id,id)
);
ALTER TABLE sales_calibration_cohorts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_calibration_cohorts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_calibration_cohorts;
CREATE POLICY tenant_isolation ON sales_calibration_cohorts
 USING(tenant_id=current_setting('app.tenant_id',true)) WITH CHECK(tenant_id=current_setting('app.tenant_id',true));
ALTER TABLE sales_calibration_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_calibration_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_calibration_items;
CREATE POLICY tenant_isolation ON sales_calibration_items
 USING(tenant_id=current_setting('app.tenant_id',true)) WITH CHECK(tenant_id=current_setting('app.tenant_id',true));
ALTER TABLE sales_calibration_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_calibration_assessments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_calibration_assessments;
CREATE POLICY tenant_isolation ON sales_calibration_assessments
 USING(tenant_id=current_setting('app.tenant_id',true)) WITH CHECK(tenant_id=current_setting('app.tenant_id',true));
