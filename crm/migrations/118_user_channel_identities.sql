-- Who may drive this instance from a channel, and the one-shot grant that says so.
--
-- Until now "may this sender run the agent?" had two answers and neither was a
-- table. Telegram's lived in `tenant_users.telegram_user_id`, a column named
-- after one product. Slack's lived in two comma-separated environment
-- variables, and `_authorized` returned TRUE when NEITHER was set -- so the
-- default posture of a joined workspace was that any member could drive the
-- main agent. A third channel would have invented a third answer.
--
-- WHY NOT `contact_identifiers` (migration 001). It looks like the same shape
-- -- a channel, a handle, a person -- and it is emphatically not. It is the
-- CRM's rolodex: it maps a handle to a `person_id` so the agent can tell who
-- an email is from, and rows arrive there by INGESTION, not by a decision. It
-- has no `revoked_at` and no `paired_by`, because nothing there was ever
-- granted. Reusing it would make "we know who this person is" and "this person
-- may operate the instance" the same fact, and every mailing list the agent
-- ever parsed would become an authorization. That conflation is precisely what
-- this table exists to prevent.
--
-- `user_id` is TEXT, not a UUID FK. A binding has to be able to name either a
-- `tenant_users.user_id` (Telegram's source of truth, and still the row
-- `lookup_user` reads) or a `user_accounts.id` (the bridge's). A UUID column
-- would silently exclude the first, and a second nullable FK column would make
-- "exactly one of these is set" an invariant nothing enforces.
--
-- The live-row uniqueness is PARTIAL -- `WHERE revoked_at IS NULL`. A full
-- unique index would mean that revoking somebody barred that native id from
-- ever pairing again, which is the opposite of what revoke is for: it would
-- turn a reversible decision into a permanent one, discoverable only by the
-- person it locked out.

BEGIN;

CREATE TABLE IF NOT EXISTS user_channel_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    -- Either a tenant_users.user_id or a user_accounts.id -- see the header.
    user_id         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    -- The platform's own id for the sender (a Slack user id, a Telegram user
    -- id). Never a display name: those are chosen by the person they identify
    -- and two people may present the same one.
    native_id       TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    -- What the operator granted, recorded on the row that records the grant.
    --
    -- Here rather than derived from `user_accounts.role` or
    -- `tenant_users.role`, because a binding may legitimately name a user
    -- neither table holds a row for -- and the alternative, defaulting to a
    -- role when that join misses, is exactly the fabrication this gate exists
    -- to stop: an identity nobody can account for would reach the runner
    -- carrying `member` because a query returned nothing. The CHECK is the
    -- same set `accounts.JIT_PROVISIONABLE_ROLES` names, so a privileged role
    -- cannot be written here even by a caller that skipped the DAL.
    --
    -- The column default is `viewer` for the same reason the DAL's is: `member`
    -- reads like a cap and is not one. Its seeded policy is
    -- ("member", "*", "allow"), and migration 088 narrows that to read-only only
    -- for the `__default__` tenant -- so on any other tenant a "capped" pairing
    -- granted every tool. Nothing relies on this default today (every insert
    -- names a role), and it is here so a hand-written INSERT during an incident
    -- lands on the narrow role rather than the wide one.
    role            TEXT NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('member', 'viewer')),
    paired_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- WHO decided. `operator:<actor id>` for a bridge approval, `cli:<user>`
    -- for one from the operator's shell. A channel can never produce a value
    -- this column will accept -- see robothor/engine/channels/identities.py.
    paired_by       TEXT NOT NULL,
    revoked_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_channel_identities_live
    ON user_channel_identities (tenant_id, channel, native_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_channel_identities_user
    ON user_channel_identities (tenant_id, channel, user_id);

-- Tenant isolation, in the permissive-when-unbound shape of 081/106: a
-- connection that never sets app.tenant_id (migrations, psql, the CLI) keeps
-- working; one that does is confined. WITH CHECK matters more here than on
-- most tables -- a confined caller that could still INSERT into another
-- tenant would not be leaking a row, it would be granting access.
ALTER TABLE user_channel_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_channel_identities FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON user_channel_identities;
CREATE POLICY tenant_isolation ON user_channel_identities
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

-- ── The grant ───────────────────────────────────────────────────────────────
--
-- A one-shot, short-lived grant, the same shape `sso_binding_grants`
-- (migration 071) already uses for binding an account to an IdP identity: mint,
-- spend once with a conditional UPDATE ... RETURNING, expire on a clock the
-- database owns.
--
-- `code_hash`, never `code`. The value is a sha256 hex digest, exactly as
-- `user_sessions.refresh_token_hash` stores a refresh token. A pairing code IS
-- a credential for the ten minutes it lives -- anyone holding it can be bound
-- to an identity -- so a database dump, a replica, or a backup must not carry
-- a spendable one. The plaintext exists only in the reply the sender received.
--
-- The live-row uniqueness is partial for the same reason as above, but the
-- clauses differ: here a row stops being live when it is SPENT or DENIED, so a
-- sender whose code was denied can be sent a fresh one rather than being stuck
-- with a dead grant until it expires.

CREATE TABLE IF NOT EXISTS channel_pairing_codes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    channel         TEXT NOT NULL,
    native_id       TEXT NOT NULL,
    code_hash       TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    denied_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_pairing_codes_live
    ON channel_pairing_codes (tenant_id, channel, native_id)
    WHERE used_at IS NULL AND denied_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_channel_pairing_codes_hash
    ON channel_pairing_codes (tenant_id, code_hash);

ALTER TABLE channel_pairing_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_pairing_codes FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON channel_pairing_codes;
CREATE POLICY tenant_isolation ON channel_pairing_codes
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
