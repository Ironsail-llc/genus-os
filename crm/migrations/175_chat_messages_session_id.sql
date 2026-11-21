-- Migration 175: index chat_messages by session, then id.
--
-- A conversation's history page (robothor/engine/chat_store.py,
-- load_history_page) reads one session's messages newest first by id. The
-- only per-session index was (session_id, created_at), so the planner walked
-- the table's primary key backwards and discarded every other session's rows:
-- a quiet workspace's page cost as much as everything written since it was
-- last active. This index serves the query directly.
--
-- Idempotent. CONCURRENTLY is not used: the migration runner applies each
-- file in a transaction, and the build is one pass over chat_messages.
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id
    ON chat_messages (session_id, id);

-- Rollback:
--   DROP INDEX IF EXISTS idx_chat_messages_session_id;
