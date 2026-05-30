-- Migration 077: dedup + unique constraint on message_reactions (BUG-4).
--
-- Without this, repeated reaction updates for the same (chat, message, reactor)
-- accumulate rows, and a retracted/changed reaction leaves a stale verdict that
-- the judge's window-mood fallback keeps counting. Dedup to the newest row per
-- key, then enforce uniqueness so the insert path can upsert and removals can
-- delete exactly one row.

DELETE FROM message_reactions a
USING message_reactions b
WHERE a.id < b.id
  AND a.tenant_id = b.tenant_id
  AND a.chat_id = b.chat_id
  AND a.message_id = b.message_id
  AND a.reactor IS NOT DISTINCT FROM b.reactor;

CREATE UNIQUE INDEX IF NOT EXISTS uq_message_reactions_reactor
    ON message_reactions (tenant_id, chat_id, message_id, reactor);
