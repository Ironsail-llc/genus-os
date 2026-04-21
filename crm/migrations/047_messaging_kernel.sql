-- ─────────────────────────────────────────────────────────────────────────────
-- 047_messaging_kernel.sql
--
-- Phase 1b of the Unified Person Graph. Channel-agnostic cross-channel
-- messaging fabric, modeled on Twenty CRM's messaging module.
--
-- Five new tables:
--   - connected_account     each tenant's mailbox / phone number / bot
--   - message_thread        groups messages (email thread, telegram chat, sms convo)
--   - message               one row per message, all channels
--   - message_participant   junction (message × role × person) — supports to/cc/bcc
--   - message_attachment    files attached to a message
--
-- Why participants are a junction (and not a column on message):
--   * One email can have many to/cc/bcc recipients, each independently linked.
--   * Group telegram chats have many participants per message.
--   * Each participant resolves independently to a CRM person via
--     resolve_contact() — handles unknown senders and partial matches cleanly.
--   * "All messages for person X" becomes:
--         SELECT m.* FROM message m
--           JOIN message_participant p ON p.message_id = m.id
--          WHERE p.person_id = $1;
--
-- Channel ∈ {email, sms, telegram, webchat, ...}. Adding a channel means
-- adding a write-through hook; no schema change.
--
-- Rollback:
--   DROP TABLE IF EXISTS message_attachment;
--   DROP TABLE IF EXISTS message_participant;
--   DROP TABLE IF EXISTS message;
--   DROP TABLE IF EXISTS message_thread;
--   DROP TABLE IF EXISTS connected_account;
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── connected_account ───────────────────────────────────────────────────────
-- The tenant's mailbox, phone number, or bot. Every message FK's to one,
-- so we always know which inbox/line/bot a message came through.
CREATE TABLE IF NOT EXISTS connected_account (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    provider TEXT NOT NULL,            -- 'gmail' | 'twilio_sms' | 'twilio_voice' | 'telegram_bot' | 'webchat'
    identifier TEXT NOT NULL,          -- email address | phone number | bot username | webchat origin
    display_name TEXT,
    oauth_token_ref TEXT,              -- secret reference (e.g. 'sops:gws-token-robothor'); never the raw token
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'error', 'revoked')),
    last_sync_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, provider, identifier)
);

CREATE INDEX IF NOT EXISTS idx_connected_account_tenant
    ON connected_account(tenant_id, provider);


-- ── message_thread ──────────────────────────────────────────────────────────
-- A conversation thread. For email this is the Gmail thread_id; for telegram
-- it's the chat_id; for sms it's a synthetic id keyed on the two phone numbers.
CREATE TABLE IF NOT EXISTS message_thread (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    channel TEXT NOT NULL,             -- 'email' | 'sms' | 'telegram' | 'webchat'
    external_thread_id TEXT NOT NULL,  -- Gmail thread_id, Telegram chat_id, etc.
    subject TEXT,
    last_message_at TIMESTAMPTZ,
    message_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel, external_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_message_thread_recent
    ON message_thread(tenant_id, last_message_at DESC NULLS LAST);


-- ── message ─────────────────────────────────────────────────────────────────
-- One row per message, regardless of channel. Bodies live here; participants
-- and attachments link via FK.
CREATE TABLE IF NOT EXISTS message (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    thread_id UUID NOT NULL REFERENCES message_thread(id) ON DELETE CASCADE,
    connected_account_id UUID REFERENCES connected_account(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,             -- denormalized from thread for fast filtering
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound', 'internal')),
    external_message_id TEXT,          -- Gmail message_id, Telegram message_id, Twilio sid; nullable for webchat
    in_reply_to TEXT,                  -- RFC 822 In-Reply-To, or platform reply pointer
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    snippet TEXT,                       -- first ~200 chars, denormalized for feed previews
    occurred_at TIMESTAMPTZ NOT NULL,  -- when the message was sent/received (not when row was created)
    raw JSONB,                          -- original platform payload, for debugging / re-extraction
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel, external_message_id)
);

CREATE INDEX IF NOT EXISTS idx_message_thread
    ON message(thread_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_message_tenant_recent
    ON message(tenant_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_message_channel_recent
    ON message(tenant_id, channel, occurred_at DESC);


-- ── message_participant ─────────────────────────────────────────────────────
-- Junction: each (message, role, person) is one row. handle is the raw
-- channel identifier (email address, phone, telegram user id); person_id
-- is populated by resolve_contact() and may be NULL for unknown senders.
CREATE TABLE IF NOT EXISTS message_participant (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    message_id UUID NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc', 'bcc', 'sender', 'reply_to')),
    person_id UUID REFERENCES crm_people(id) ON DELETE SET NULL,
    handle TEXT NOT NULL,              -- email address, phone, telegram user id
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_participant_person
    ON message_participant(person_id, message_id)
    WHERE person_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_message_participant_message
    ON message_participant(message_id);

CREATE INDEX IF NOT EXISTS idx_message_participant_handle
    ON message_participant(tenant_id, handle);


-- ── message_attachment ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS message_attachment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT,
    storage_path TEXT,                 -- local path or s3 key, when downloaded
    external_attachment_id TEXT,       -- Gmail attachment id, Telegram file_id, Twilio media url
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_attachment_message
    ON message_attachment(message_id);

COMMIT;
