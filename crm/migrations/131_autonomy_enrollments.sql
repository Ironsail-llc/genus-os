-- Authenticated, owner-bound intake intents. No raw tokens or input values.
CREATE TABLE IF NOT EXISTS autonomy_enrollments (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    kind text NOT NULL CHECK (kind IN ('profile','credential','document','totp','payment_card')),
    origin text,
    expires_at timestamptz NOT NULL,
    resource_id uuid REFERENCES vault_resources(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK ((resource_id IS NULL) = (completed_at IS NULL))
);
CREATE INDEX IF NOT EXISTS autonomy_enrollments_owner ON autonomy_enrollments(tenant_id,owner_id);
