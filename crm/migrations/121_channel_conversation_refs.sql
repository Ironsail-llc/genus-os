-- Where a channel sends when it wants to reach somebody again.
--
-- Telegram and Slack address a person from an id: a chat id IS an address. The
-- Bot Framework family cannot. Sending to a Teams user needs the tenant's own
-- `serviceUrl` (regional, and rotated by Microsoft) plus the conversation id
-- their platform minted, and both arrive only on an inbound activity. A channel
-- with nowhere to write them down can never reply, never post a briefing, and
-- never even deliver the pairing code that would make the sender known.
--
-- WHY NOT A COLUMN ON `user_channel_identities` (migration 118). Two reasons.
--
-- A reference has to exist for a sender who is NOT paired. The pairing code is
-- itself a message, and it goes to the conversation this row names. An identity
-- row is only created by an operator-gated approval, so a reference that could
-- only live beside one would make pairing itself undeliverable.
--
-- And a reference is ROUTING, which is not authorization. Every value here comes
-- from the sender's own platform. 118 exists to keep anything reachable from an
-- inbound message away from the columns that decide what a person may do; a
-- write path that touched `role` and `service_url` in one statement is exactly
-- the conflation it was written to prevent. So: a separate table, with no
-- `role`, no `user_id` and no `paired_by` -- and the engine's COLUMNS tuple is
-- asserted against that set in the suite.
--
-- ONE LIVE ROW PER SENDER, and a full unique constraint rather than 118's
-- partial one, because there is nothing to revoke here: the newest conversation
-- is the only one a reply belongs in, and a kept-around older `serviceUrl` is
-- how a proactive send ends up POSTing to a region the tenant has left.

BEGIN;

CREATE TABLE IF NOT EXISTS channel_conversation_refs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    -- The channel name, as the registry resolves it (`teams`).
    channel         TEXT NOT NULL,
    -- The platform's own id for the sender. For Teams this is the stable
    -- directory object id where one is available, so a reference survives the
    -- person's Teams client being reinstalled.
    native_id       TEXT NOT NULL,
    -- The conversation the platform minted. A 1:1 chat, a channel thread, or a
    -- group chat -- the channel does not distinguish, and neither does this.
    conversation_id TEXT NOT NULL,
    -- The regional endpoint activities for this conversation are POSTed to.
    -- HTTPS is enforced by the DAL before any INSERT: the next proactive send
    -- carries a bearer token to whatever is stored here.
    service_url     TEXT NOT NULL DEFAULT '',
    -- What the sender's platform calls them. Convenience for the operator
    -- reviewing what the instance has recorded; never used to identify anybody.
    display_name    TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_conversation_refs
    ON channel_conversation_refs (tenant_id, channel, native_id);

CREATE INDEX IF NOT EXISTS idx_channel_conversation_refs_conversation
    ON channel_conversation_refs (tenant_id, channel, conversation_id);

-- Tenant isolation, in the permissive-when-unbound shape of 081/106/118: a
-- connection that never sets app.tenant_id (migrations, psql, the CLI) keeps
-- working; one that does is confined. WITH CHECK matters because a confined
-- caller able to INSERT into another tenant could redirect that tenant's next
-- proactive message at a conversation of its own choosing.
ALTER TABLE channel_conversation_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_conversation_refs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON channel_conversation_refs;
CREATE POLICY tenant_isolation ON channel_conversation_refs
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
