-- Migration 177: memory_segments -- raw conversation, segmented by topic.
--
-- A segment is a contiguous run of turns from one source (a chat session
-- today), split where the subject changes (robothor/memory/segments.py). Its
-- text is the turns verbatim with role labels, after the same credential
-- redaction chat_store applies before embedding. Recall searches it with a
-- vector leg and a keyword leg fused by reciprocal rank, as memory_facts and
-- kb_chunks are searched, so a detail fact extraction dropped is still found.
--
-- source_kind / source_ref: where the turns came from. 'conversation' is a
--   chat session and source_ref its session_key; 'document' and 'email' are
--   reserved. start_ref / end_ref: the first and last chat_messages.id the
--   segment covers. part: 0, except for a single turn longer than the size
--   budget, which is split (never clipped) into consecutive segments with the
--   same start_ref/end_ref and part 0, 1, 2 ...; their texts, label removed,
--   concatenate to the turn. (tenant_id, source_kind, source_ref, start_ref,
--   part) is unique so re-segmenting a conversation upserts instead of
--   duplicating.
-- started_at / ended_at: event time of the first and last turn.
-- chat_session_id: the session a conversation segment copies. ON DELETE
--   CASCADE, so a segment never outlives its conversation: clearing a
--   session, or the stale-session sweep, removes its segments with its
--   messages. The nightly chat TTL prunes segments past the same 90 days.
-- person_id: the session's person (chat_sessions.person_id).
-- scope_kind / scope_id: who may read the segment. A session that belongs to
--   one person is private to that person: 'user' and scope_id = that person's
--   id. A session with no single person (a workspace, a group channel, a
--   system session) is 'shared', and a workspace's segments reach only its
--   members. Applied on every read, whatever the data-scoping rollout says.
--
-- Idempotent: every statement can run twice.

CREATE TABLE IF NOT EXISTS memory_segments (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES crm_tenants(id),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('conversation', 'document', 'email')),
    source_ref TEXT NOT NULL,
    start_ref BIGINT NOT NULL,
    end_ref BIGINT NOT NULL,
    part SMALLINT NOT NULL DEFAULT 0 CHECK (part >= 0),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    topic TEXT,
    text TEXT NOT NULL,
    embedding vector(1024),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    scope_kind TEXT NOT NULL DEFAULT 'shared'
        CHECK (scope_kind IN ('shared', 'user', 'agent')),
    scope_id TEXT,
    person_id UUID REFERENCES crm_people(id) ON DELETE SET NULL,
    chat_session_id BIGINT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (start_ref <= end_ref),
    UNIQUE (tenant_id, source_kind, source_ref, start_ref, part)
);

-- Same index shapes as memory_facts (migration 023).
CREATE INDEX IF NOT EXISTS idx_memory_segments_tsv ON memory_segments USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_memory_segments_embedding ON memory_segments
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);
-- The retention sweep and the since/until filter read event time per tenant.
CREATE INDEX IF NOT EXISTS idx_memory_segments_tenant_started
    ON memory_segments (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_memory_segments_chat_session
    ON memory_segments (chat_session_id);

ALTER TABLE memory_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_segments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memory_segments;
CREATE POLICY tenant_isolation ON memory_segments
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS memory_segments;
