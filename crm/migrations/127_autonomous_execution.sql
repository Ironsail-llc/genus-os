BEGIN;

CREATE TABLE IF NOT EXISTS autonomy_key_versions (
    id TEXT PRIMARY KEY,
    encrypted_key BYTEA NOT NULL,
    active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS autonomy_one_active_key ON autonomy_key_versions(active) WHERE active;

-- Personal resources are deliberately outside vault_secrets: the service-env
-- exporter and the general agent vault_get path must not be able to read them.
CREATE TABLE IF NOT EXISTS vault_resources (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('profile','credential','document','totp','browser_session','payment_card')),
    label TEXT NOT NULL,
    origin TEXT,
    encrypted_value BYTEA NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vault_resources_owner ON vault_resources(tenant_id, owner_id);
ALTER TABLE vault_resources ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS autonomy_grants (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    policy JSONB NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS autonomy_grants_owner ON autonomy_grants(tenant_id, owner_id);

CREATE TABLE IF NOT EXISTS autonomy_operations (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    grant_id UUID NOT NULL REFERENCES autonomy_grants(id),
    grant_version INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    proposal JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('reserved','submitting','reconciling','completed','failed','cancelled','awaiting_input')),
    evidence JSONB,
    execution_plan JSONB,
    input_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS autonomy_operations_owner
    ON autonomy_operations(tenant_id, owner_id, created_at);
ALTER TABLE autonomy_operations ADD COLUMN IF NOT EXISTS execution_plan JSONB;
ALTER TABLE autonomy_operations ADD COLUMN IF NOT EXISTS input_reason TEXT;

CREATE TABLE IF NOT EXISTS autonomy_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id UUID NOT NULL,
    event TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS autonomy_events_owner ON autonomy_events(tenant_id, owner_id, id);

CREATE TABLE IF NOT EXISTS autonomy_settings (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    settings JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,owner_id)
);

-- Match the platform tenant backstop; DAL also requires tenant and owner.
ALTER TABLE vault_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_resources FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON vault_resources;
CREATE POLICY tenant_isolation ON vault_resources
USING (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true))
WITH CHECK (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE autonomy_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_grants;
CREATE POLICY tenant_isolation ON autonomy_grants
USING (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true))
WITH CHECK (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE autonomy_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_operations;
CREATE POLICY tenant_isolation ON autonomy_operations
USING (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true))
WITH CHECK (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE autonomy_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_events;
CREATE POLICY tenant_isolation ON autonomy_events
USING (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true))
WITH CHECK (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE autonomy_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON autonomy_settings;
CREATE POLICY tenant_isolation ON autonomy_settings
USING (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true))
WITH CHECK (current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true));

COMMIT;
