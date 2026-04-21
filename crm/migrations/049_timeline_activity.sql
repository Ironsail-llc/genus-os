-- ─────────────────────────────────────────────────────────────────────────────
-- 049_timeline_activity.sql
--
-- Phase 1d of the Unified Person Graph. The denormalized append-only feed
-- that powers Contact 360.
--
-- Why denormalized:
--   The unified contact view is a hot read path (every contact-detail open,
--   every agent get_contact_360 call). Computing it on the fly requires a
--   9-way UNION across message, call_log, calendar_event_participant,
--   crm_tasks, crm_notes, agent_runs, memory_facts, chat_messages,
--   crm_messages — slow to plan, slow to execute, hard to paginate.
--
--   timeline_activity holds one row per touch with just enough fields to
--   render a feed item: title, snippet, occurred_at, channel, direction,
--   activity_type, plus source_table+source_id to drill into the detail
--   row when needed. Read path becomes a single index scan on
--   (tenant_id, person_id, occurred_at DESC).
--
-- Write path:
--   Each per-channel write-through hook emits one row here in addition to
--   the detail-table insert. Failures here do not block the channel write
--   (best-effort logging — see the channel_bus pattern).
--
-- One initial backfill at the end of this migration seeds rows from the
-- per-channel tables that already exist (crm_tasks, crm_notes,
-- crm_conversations/messages, channel_message_map) so historical data is
-- visible from day one.
--
-- Rollback:
--   DROP TABLE IF EXISTS timeline_activity;
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

CREATE TABLE IF NOT EXISTS timeline_activity (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    person_id UUID REFERENCES crm_people(id) ON DELETE CASCADE,

    occurred_at TIMESTAMPTZ NOT NULL,
    activity_type TEXT NOT NULL CHECK (activity_type IN (
        'email', 'sms', 'telegram_message', 'webchat_message',
        'voice_call', 'calendar_event',
        'note', 'task', 'task_status_change',
        'agent_run', 'memory_fact', 'conversation_message'
    )),

    source_table TEXT NOT NULL,        -- 'message' | 'call_log' | 'calendar_event' | 'crm_tasks' | ...
    source_id TEXT NOT NULL,           -- stringified id (UUID, BIGINT, SERIAL — varies by table)

    channel TEXT,                       -- 'email' | 'sms' | 'telegram' | 'webchat' | 'voice' | 'calendar'
    direction TEXT,                     -- 'inbound' | 'outbound' | NULL
    title TEXT,                         -- one-line label for the feed item
    snippet TEXT,                       -- ~200-char preview
    agent_id TEXT,                      -- which agent produced this (if any)

    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_timeline_person
    ON timeline_activity(tenant_id, person_id, occurred_at DESC)
    WHERE person_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_timeline_recent
    ON timeline_activity(tenant_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_timeline_type
    ON timeline_activity(tenant_id, activity_type, occurred_at DESC);


-- ── Initial backfill from existing per-channel tables ───────────────────────
-- crm_tasks
INSERT INTO timeline_activity
    (tenant_id, person_id, occurred_at, activity_type, source_table, source_id,
     channel, title, snippet, metadata)
SELECT
    t.tenant_id,
    t.person_id,
    t.created_at,
    'task',
    'crm_tasks',
    t.id::text,
    NULL,
    COALESCE(NULLIF(t.title, ''), '(untitled task)'),
    LEFT(COALESCE(t.body, ''), 200),
    jsonb_build_object('status', t.status)
  FROM crm_tasks t
 WHERE t.person_id IS NOT NULL
   AND t.deleted_at IS NULL
ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING;

-- crm_notes
INSERT INTO timeline_activity
    (tenant_id, person_id, occurred_at, activity_type, source_table, source_id,
     channel, title, snippet)
SELECT
    n.tenant_id,
    n.person_id,
    n.created_at,
    'note',
    'crm_notes',
    n.id::text,
    NULL,
    COALESCE(NULLIF(n.title, ''), '(untitled note)'),
    LEFT(COALESCE(n.body, ''), 200)
  FROM crm_notes n
 WHERE n.person_id IS NOT NULL
   AND n.deleted_at IS NULL
ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING;

-- crm_conversations + crm_messages (legacy chatwoot-style)
INSERT INTO timeline_activity
    (tenant_id, person_id, occurred_at, activity_type, source_table, source_id,
     channel, direction, snippet, metadata)
SELECT
    COALESCE(c.tenant_id, 'default'),
    c.person_id,
    msg.created_at,
    'conversation_message',
    'crm_messages',
    msg.id::text,
    c.inbox_name,
    CASE msg.message_type WHEN 'incoming' THEN 'inbound' WHEN 'outgoing' THEN 'outbound' END,
    LEFT(COALESCE(msg.content, ''), 200),
    jsonb_build_object(
        'sender_name', msg.sender_name,
        'sender_type', msg.sender_type
    )
  FROM crm_messages msg
  JOIN crm_conversations c ON c.id = msg.conversation_id
 WHERE c.person_id IS NOT NULL
ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING;

-- channel_message_map (telegram traffic that has person_id from migration 046)
INSERT INTO timeline_activity
    (tenant_id, person_id, occurred_at, activity_type, source_table, source_id,
     channel, direction, agent_id, snippet)
SELECT
    cmm.tenant_id,
    cmm.person_id,
    cmm.created_at,
    'telegram_message',
    'channel_message_map',
    cmm.id::text,
    cmm.channel,
    cmm.direction,
    NULLIF(cmm.author_agent_id, 'user'),
    NULL  -- snippet pulled lazily from chat_messages.message JSONB on render
  FROM channel_message_map cmm
 WHERE cmm.person_id IS NOT NULL
ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING;

-- agent_runs (telegram-triggered runs that have person_id from migration 046)
INSERT INTO timeline_activity
    (tenant_id, person_id, occurred_at, activity_type, source_table, source_id,
     agent_id, title, snippet, metadata)
SELECT
    COALESCE(ar.tenant_id, 'default'),
    ar.person_id,
    ar.created_at,
    'agent_run',
    'agent_runs',
    ar.id::text,
    ar.agent_id,
    ar.agent_id || ' (' || ar.status || ')',
    LEFT(COALESCE(ar.output_text, ''), 200),
    jsonb_build_object(
        'status', ar.status,
        'duration_ms', ar.duration_ms,
        'trigger_type', ar.trigger_type
    )
  FROM agent_runs ar
 WHERE ar.person_id IS NOT NULL
ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING;

COMMIT;
