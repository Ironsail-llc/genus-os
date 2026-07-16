-- crm/migrations/085_sso_binding_grants.sql
-- One-shot, TTL'd grants that let an operator explicitly authorize binding an
-- EXISTING account (e.g. the bootstrapped owner) to an SSO identity. Without a
-- live grant, `jit_provision` keeps refusing email-matched accounts
-- (AccountBindingRequiredError): a verified email claim alone must never adopt
-- a pre-existing or privileged account. Consumption is a single atomic UPDATE
-- (used_at stamp) so concurrent sign-ins cannot both spend one grant.
--
-- Rollback: DROP TABLE IF EXISTS sso_binding_grants;

CREATE EXTENSION IF NOT EXISTS citext;  -- no-op since 071; keeps 085 self-sufficient

CREATE TABLE IF NOT EXISTS sso_binding_grants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL REFERENCES crm_tenants(id),
    email           CITEXT NOT NULL,
    -- Optional IdP pin: when set, only a verified claim from this issuer can
    -- consume the grant. NULL = any allowlisted IdP.
    issuer          TEXT,
    reason          TEXT,
    created_by      TEXT NOT NULL DEFAULT 'cli',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    used_by_issuer  TEXT,
    used_by_subject TEXT,
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sso_binding_grants_pending
    ON sso_binding_grants (tenant_id, email)
    WHERE used_at IS NULL AND revoked_at IS NULL;
