-- Durable agent-to-agent messages: the fabric behind the multi-agent claim.
--
-- WHY. Inter-agent messaging lived in Redis lists with a ONE HOUR TTL and no
-- acknowledgement: a message to a busy agent silently evaporated if the
-- recipient did not poll within the hour, an engine restart between send and
-- receive lost nothing observable, and nothing recorded that a message ever
-- existed. Real coordination therefore routed around the feature entirely
-- (CRM tasks by convention). The 2026-08-24 competitive sweep called the
-- ephemeral fabric the one thing eroding an otherwise-leading orchestration
-- story.
--
-- Postgres is the store; Redis keeps exactly one job — the real-time wake
-- publish — because a pub/sub ping that gets lost costs a poll delay, not a
-- message.
--
-- DELIVERY SEMANTICS. receive() marks delivered_at and returns oldest-first;
-- a message is handed out once (the UPDATE ... RETURNING claims it
-- atomically, so concurrent receivers cannot double-deliver). Undelivered
-- messages survive restarts until retention: delivered rows purge after 7
-- days, undelivered after 30 — an inbox nobody drains for a month is a dead
-- recipient, and the purge logs what it drops.
--
-- RLS. Migration 081's backstop policy attaches to every table with a
-- tenant_id column when it runs — but it already ran. New tables bring their
-- own policy, same shape, so isolation does not depend on anyone re-running
-- 081.

CREATE TABLE IF NOT EXISTS agent_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL REFERENCES crm_tenants(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    content TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    acked_at TIMESTAMPTZ
);

-- The inbox poll: undelivered messages for one agent, oldest first.
CREATE INDEX IF NOT EXISTS idx_agent_messages_inbox
    ON agent_messages (tenant_id, to_agent, created_at)
    WHERE delivered_at IS NULL;

-- Retention sweeps scan by age.
CREATE INDEX IF NOT EXISTS idx_agent_messages_created
    ON agent_messages (created_at);

ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON agent_messages;
-- Same shape as 081's backstop, deliberately: permissive when no tenant is
-- bound (tests, admin scripts), enforcing the moment a connection binds one.
CREATE POLICY tenant_isolation ON agent_messages
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
