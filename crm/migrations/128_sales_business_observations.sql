-- Current business evidence is distinct from historical milestone notifications.
CREATE UNIQUE INDEX IF NOT EXISTS sales_prospects_tenant_identity ON sales_prospects(tenant_id,id);
CREATE TABLE IF NOT EXISTS sales_business_observations (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('practice','signup','order')), external_id TEXT NOT NULL,
 revision TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, observed_at TIMESTAMPTZ NOT NULL,
 data JSONB NOT NULL, data_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,source,account_id,kind,external_id)
);
CREATE TABLE IF NOT EXISTS sales_business_observation_history (
 tenant_id TEXT NOT NULL, observation_id UUID NOT NULL, version INTEGER NOT NULL,
 revision TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL, data_hash TEXT NOT NULL,
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,observation_id,version),
 FOREIGN KEY(tenant_id,observation_id) REFERENCES sales_business_observations(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS sales_customer_bindings (
 tenant_id TEXT NOT NULL, observation_id UUID NOT NULL, prospect_id UUID NOT NULL,
 reviewed_revision TEXT NOT NULL, identity_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('confirmed','held')), reviewed_by TEXT NOT NULL,
 reason TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,observation_id),
 FOREIGN KEY(tenant_id,observation_id) REFERENCES sales_business_observations(tenant_id,id),
 FOREIGN KEY(tenant_id,prospect_id) REFERENCES sales_prospects(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS sales_business_practice ON sales_business_observations
 (tenant_id,source,account_id,(data->>'practice_id'));
CREATE INDEX IF NOT EXISTS sales_bindings_prospect ON sales_customer_bindings(tenant_id,prospect_id);
CREATE INDEX IF NOT EXISTS sales_business_history_revision ON sales_business_observation_history(tenant_id,observation_id,revision);

ALTER TABLE sales_business_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_business_observations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_business_observations;
CREATE POLICY tenant_isolation ON sales_business_observations
 USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
ALTER TABLE sales_business_observation_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_business_observation_history FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_business_observation_history;
CREATE POLICY tenant_isolation ON sales_business_observation_history
 USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
ALTER TABLE sales_customer_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_customer_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_customer_bindings;
CREATE POLICY tenant_isolation ON sales_customer_bindings
 USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
