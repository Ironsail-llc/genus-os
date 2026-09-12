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
    ADD COLUMN IF NOT EXISTS password_updated_at TIMESTAMPTZ,
    -- RFC 6238 §5.2: a one-time password must be accepted ONCE. The verifier
    -- has a ±1 step window, so without this the same six digits work for 90
    -- seconds and a shoulder-surfed or replayed code is not "one-time" at all.
    ADD COLUMN IF NOT EXISTS mfa_last_used_step BIGINT;

COMMENT ON COLUMN user_accounts.mfa_enabled IS
    'TRUE = a confirmed TOTP factor is required after a correct password.';
COMMENT ON COLUMN user_accounts.mfa_secret_enc IS
    'Encrypted (AES-256-GCM) base32 TOTP secret. NEVER served over HTTP after enrollment.';
COMMENT ON COLUMN user_accounts.failed_login_count IS
    'Consecutive failed local sign-ins. Reset to 0 on success.';
COMMENT ON COLUMN user_accounts.locked_until IS
    'Local sign-in refused until this moment (set after repeated failures).';
COMMENT ON COLUMN user_accounts.mfa_last_used_step IS
    'Highest accepted TOTP time step. A code at or below it is a replay and is refused.';

-- ── Canonical (lower-case) email storage ────────────────────────────
--
-- Sign-in casefolds the submitted address, but `bootstrap_owner_account` and
-- `genus user add` stored whatever the operator typed. On THIS schema the
-- column is CITEXT (migration 071), so the lookup still matched and sign-in
-- worked -- but that is a property of one column type, not of the code, and a
-- deployment that ever loses citext would silently lock the owner out of their
-- own appliance. Canonicalising on write plus lower-casing what is already
-- stored makes the guarantee independent of the column type, and keeps the
-- lookup on `email = %s` so it still uses the (tenant_id, email) unique index
-- rather than a sequential scan on an unauthenticated route.
--
-- Collisions are SKIPPED, never merged: two rows differing only by case are
-- two accounts with two password hashes and two roles, and picking a winner
-- here could hand one person the other's session. A NOTICE names them so an
-- operator can resolve it deliberately.
DO $$
DECLARE
    collided TEXT;
BEGIN
    FOR collided IN
        SELECT lower(email::text) FROM user_accounts
        GROUP BY tenant_id, lower(email::text) HAVING count(*) > 1
    LOOP
        RAISE NOTICE 'migration 114: % has rows differing only by case; left as-is', collided;
    END LOOP;

    UPDATE user_accounts u
       SET email = lower(u.email::text), updated_at = NOW()
     WHERE u.email::text <> lower(u.email::text)
       AND NOT EXISTS (
           SELECT 1 FROM user_accounts o
            WHERE o.tenant_id = u.tenant_id
              AND o.id <> u.id
              AND lower(o.email::text) = lower(u.email::text)
       );
END $$;

-- A functional unique index adds a guarantee only where the column is NOT
-- citext; on citext the existing UNIQUE (tenant_id, email) already collapses
-- case, and a second index on the login table would be pure write cost.
--
-- The type is read through `user_accounts::regclass`, i.e. the table the
-- search_path actually resolves to. An unqualified `information_schema.columns
-- WHERE table_name = 'user_accounts'` matches EVERY schema that has such a
-- table, so on a box with a scratch copy around it answers about the wrong one
-- — which is how this guard first tested "correct" while doing nothing.
--
-- A pre-existing pair of rows differing only by case cannot satisfy the index.
-- That is a condition to report, not to abort a migration over: the columns
-- above are what local login actually needs, and wedging the upgrade would
-- leave the operator with neither.
DO $$
DECLARE
    email_is_citext BOOLEAN;
BEGIN
    SELECT a.atttypid = to_regtype('citext')
      INTO email_is_citext
      FROM pg_attribute a
     WHERE a.attrelid = 'user_accounts'::regclass
       AND a.attname = 'email'
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF email_is_citext IS DISTINCT FROM TRUE THEN
        BEGIN
            EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_accounts_tenant_lower_email '
                    'ON user_accounts (tenant_id, lower(email::text))';
        EXCEPTION WHEN unique_violation THEN
            RAISE NOTICE 'migration 114: rows differing only by email case exist, so the '
                         'case-insensitive unique index was not created. Resolve those '
                         'accounts, then create it by hand.';
        END;
    END IF;
END $$;

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
--       DROP COLUMN IF EXISTS password_updated_at,
--       DROP COLUMN IF EXISTS mfa_last_used_step;
