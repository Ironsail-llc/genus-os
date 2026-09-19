-- Durable work and external effects. Money is integer micro-USD, never floats.
CREATE TABLE IF NOT EXISTS operation_jobs (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL,
 dedup_key TEXT NOT NULL, payload JSONB NOT NULL, result JSONB,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
 available_at TIMESTAMPTZ NOT NULL DEFAULT now(), deadline TIMESTAMPTZ NOT NULL,
 lease_until TIMESTAMPTZ, lease_token UUID, error TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,kind,dedup_key)
);
CREATE INDEX IF NOT EXISTS operation_jobs_ready ON operation_jobs(tenant_id,kind,available_at)
 WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS operation_budgets (
 tenant_id TEXT NOT NULL, scope TEXT NOT NULL, limit_units BIGINT NOT NULL CHECK(limit_units>=0),
 spent_units BIGINT NOT NULL DEFAULT 0 CHECK(spent_units>=0),
 reserved_units BIGINT NOT NULL DEFAULT 0 CHECK(reserved_units>=0),
 PRIMARY KEY(tenant_id,scope)
);
CREATE TABLE IF NOT EXISTS operation_reservations (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, scope TEXT NOT NULL, dedup_key TEXT NOT NULL,
 reserved_units BIGINT NOT NULL CHECK(reserved_units>=0), actual_units BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(tenant_id,scope,dedup_key),
 FOREIGN KEY(tenant_id,scope) REFERENCES operation_budgets(tenant_id,scope)
);
CREATE TABLE IF NOT EXISTS operation_inbox (
 tenant_id TEXT NOT NULL, provider TEXT NOT NULL, event_id TEXT NOT NULL,
 payload JSONB NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 processed_at TIMESTAMPTZ, PRIMARY KEY(tenant_id,provider,event_id)
);
CREATE TABLE IF NOT EXISTS operation_actions (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, dedup_key TEXT NOT NULL,
 payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'review'
 CHECK(status IN ('review','approved','rejected','executing','completed','unknown','cancelled','failed')),
 approved_hash TEXT, decided_by TEXT, decided_at TIMESTAMPTZ,
 expires_at TIMESTAMPTZ NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ,
 receipt JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,kind,dedup_key)
);
CREATE TABLE IF NOT EXISTS operation_audit (
 id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, entity_id TEXT NOT NULL,
 event TEXT NOT NULL, actor TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS operation_effects (
 tenant_id TEXT NOT NULL, kind TEXT NOT NULL, dedup_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'executing' CHECK(status IN ('executing','completed','unknown')),
 receipt JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,kind,dedup_key)
);
ALTER TABLE operation_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_jobs;
CREATE POLICY tenant_isolation ON operation_jobs
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_budgets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_budgets;
CREATE POLICY tenant_isolation ON operation_budgets
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_reservations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_reservations;
CREATE POLICY tenant_isolation ON operation_reservations
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_inbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_inbox;
CREATE POLICY tenant_isolation ON operation_inbox
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_actions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_actions;
CREATE POLICY tenant_isolation ON operation_actions
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_audit;
CREATE POLICY tenant_isolation ON operation_audit
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE operation_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_effects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON operation_effects;
CREATE POLICY tenant_isolation ON operation_effects
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));
