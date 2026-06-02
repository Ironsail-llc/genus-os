-- Migration 071: human user accounts + sessions (multi-user auth, Phase A).
--
-- Adds the credential/identity layer the platform lacked: real human users
-- who authenticate (SSO/OIDC/SAML, with optional break-glass password) and map
-- onto the EXISTING role-based authorization (role_permissions, migration 037)
-- and multi-tenancy (crm_tenants). This is the "authentication" half — the
-- "authorization" half (role_permissions + robothor/engine/permissions.py) is
-- already in place and is reused unchanged.
--
-- Distinct from tenant_users (a Telegram-routing table): user_accounts is the
-- web/SSO login identity. The two converge on the same role vocabulary and may
-- be linked (tenant_users.person_id <-> user_accounts.person_id) for one human.

-- Case-insensitive email comparisons (login is case-insensitive).
CREATE EXTENSION IF NOT EXISTS citext;

-- ── User accounts (login identities) ────────────────────────────────
CREATE TABLE IF NOT EXISTS user_accounts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT NOT NULL REFERENCES crm_tenants(id),
    email         CITEXT NOT NULL,
    -- SSO identity (set on first JIT provision). NULL for break-glass-only accounts.
    idp_issuer    TEXT,
    idp_subject   TEXT,
    -- Optional local password (break-glass admin / non-SSO). NULL for SSO-only.
    password_hash TEXT,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',  -- owner | admin | member | viewer
    status        TEXT NOT NULL DEFAULT 'active',  -- active | disabled | invited
    -- Link to the operator's CRM rolodex row (mirrors tenant_users.person_id, mig 039).
    person_id     UUID REFERENCES crm_people(id) ON DELETE SET NULL,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);

-- One human per (issuer, subject) across the platform.
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_accounts_idp
    ON user_accounts (idp_issuer, idp_subject)
    WHERE idp_issuer IS NOT NULL AND idp_subject IS NOT NULL;

-- At most one owner per tenant (mirrors the operator-uniqueness pattern, mig 039).
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_accounts_owner
    ON user_accounts (tenant_id)
    WHERE role = 'owner';

CREATE INDEX IF NOT EXISTS idx_user_accounts_tenant ON user_accounts (tenant_id);

-- ── Refresh sessions (revocable; access tokens stay stateless) ───────
-- The access token is a short-TTL stateless JWT (no DB hit on the hot path).
-- The refresh token is opaque + stored hashed here so logout / "log out
-- everywhere" / admin revocation are real.
CREATE TABLE IF NOT EXISTS user_sessions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES user_accounts(id) ON DELETE CASCADE,
    refresh_token_hash TEXT NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ,
    user_agent         TEXT,
    ip                 INET,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_sessions_refresh
    ON user_sessions (refresh_token_hash);

-- ── Role vocabulary alignment ───────────────────────────────────────
-- 037 seeded viewer/user/admin/owner. user_accounts uses 'member' as the
-- default full-access human role; seed it to mirror 'user' so the existing
-- permissions.py (role_permissions lookups) works unchanged. The legacy
-- 'user' rows stay for tenant_users backward-compat.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'member', '*', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DROP TABLE IF EXISTS user_sessions;
--   DROP TABLE IF EXISTS user_accounts;
--   DELETE FROM role_permissions WHERE tenant_id='__default__' AND role='member';
