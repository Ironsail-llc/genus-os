BEGIN;

-- Channel Bus — maps platform (Telegram) message IDs to persisted chat_messages rows.
--
-- Purpose: when any fleet agent delivers to Telegram, the delivery hook dual-writes
-- the output into the main agent's canonical chat session (so main has full visibility
-- of everything that reached the user). This table records the mapping between
-- Telegram's own message_id (returned from bot.send_message) and the chat_messages
-- row we stored, so that when the user taps "Reply" on a surfaced message we can
-- recover the original author, content, and owning run.
--
-- JSONB convention for chat_messages.message when origin='channel_bus':
--   { "role": "assistant",
--     "content": "...",
--     "author_agent_id": "devops-manager",
--     "author_display_name": "Dev Team Operations Manager",
--     "surfaced_from_run_id": "<uuid>",
--     "origin": "channel_bus" }
--
-- Inbound user turns that reply to a surfaced message gain:
--   { "role": "user",
--     "content": "...",
--     "telegram_message_id": "12345",
--     "replies_to": { "platform_message_id": "12340",
--                     "author_agent_id": "devops-manager" } }

CREATE TABLE IF NOT EXISTS channel_message_map (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    channel TEXT NOT NULL,               -- 'telegram' | 'webchat' | future platforms
    chat_id TEXT NOT NULL,               -- platform chat identifier
    platform_message_id TEXT NOT NULL,   -- Telegram message_id (stringified for portability)
    session_key TEXT NOT NULL,           -- session_key of the chat_session we wrote to (main's canonical session)
    chat_message_id BIGINT REFERENCES chat_messages(id) ON DELETE CASCADE,
    author_agent_id TEXT NOT NULL,
    author_run_id UUID,                  -- the run that produced the surfaced message
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel, chat_id, platform_message_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_map_session
    ON channel_message_map(session_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_channel_map_lookup
    ON channel_message_map(tenant_id, channel, chat_id, platform_message_id);

COMMIT;
