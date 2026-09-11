-- 114: local email + password sign-in with mandatory owner MFA.
--
-- Migration 071 gave `user_accounts` a `password_hash` column for a
-- break-glass path that was never built, so until now the only way into the
-- Helm was an external identity provider (OIDC) or Cloudflare Access. A
-- five-minute install cannot require an IdP to exist first, so local login
-- becomes a first-class sign-in method — which means it also needs the parts
-- that make a password endpoint safe to expose: a failure counter, a lockout
-- clock, a second factor, and a password-age stamp.
--
-- The TOTP secret is stored ENCRYPTED (`mfa_secret_enc`, AES-256-GCM, key
-- derived from the server-held auth signing key — see
-- robothor/auth/mfa_secrets.py). A plaintext shared secret in a table that
-- any DB-read bug or backup copy can reach is a second factor in name only.
--
-- `password_reset_tokens` stores only a SHA-256 hash of the token, the same
-- shape `user_sessions.refresh_token_hash` uses: a stolen backup must not
-- hand the thief a working reset link.

BEGIN;

-- ── Local-credential state on the existing account row ──────────────
ALTER TABLE user_accounts
    ADD COLUMN IF NOT EXISTS mfa_enabled        BOOLEAN NOT NULL DEFAULT FALSE,
    -- AES-256-GCM ciphertext, base64. NULL = no TOTP secret provisioned.
    -- A *pending* enrollment also lives here with mfa_enabled = FALSE, so an
    -- unconfirmed enrollment can never satisfy a login challenge.
    ADD COLUMN IF NOT EXISTS mfa_secret_enc     TEXT,
    ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS password_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN user_accounts.mfa_enabled IS
    'TRUE = a confirmed TOTP factor is required after a correct password.';
COMMENT ON COLUMN user_accounts.mfa_secret_enc IS
    'Encrypted (AES-256-GCM) base32 TOTP secret. NEVER served over HTTP after enrollment.';
COMMENT ON COLUMN user_accounts.failed_login_count IS
    'Consecutive failed local sign-ins. Reset to 0 on success.';
COMMENT ON COLUMN user_accounts.locked_until IS
    'Local sign-in refused until this moment (set after repeated failures).';

-- ── Password reset tokens (hash-at-rest, single use) ────────────────
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  TEXT NOT NULL,
    user_id    UUID NOT NULL REFERENCES user_accounts(id) ON DELETE CASCADE,
    -- SHA-256 of the raw token; the raw value exists only in the operator's
    -- hands. UNIQUE so a replayed hash cannot be inserted twice.
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user
    ON password_reset_tokens (user_id);

-- ── RLS, matching the tenant-scoping convention of migration 081/112 ──
-- `current_setting('app.tenant_id')` is set per connection by
-- robothor/db/connection.py; an unset value yields '' and matches nothing.
ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE password_reset_tokens FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON password_reset_tokens;
CREATE POLICY tenant_isolation ON password_reset_tokens
    USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    )
    WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    );

COMMIT;

-- Rollback:
--   DROP TABLE IF EXISTS password_reset_tokens;
--   ALTER TABLE user_accounts
--       DROP COLUMN IF EXISTS mfa_enabled,
--       DROP COLUMN IF EXISTS mfa_secret_enc,
--       DROP COLUMN IF EXISTS failed_login_count,
--       DROP COLUMN IF EXISTS locked_until,
--       DROP COLUMN IF EXISTS password_updated_at;
