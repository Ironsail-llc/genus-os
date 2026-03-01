-- 019_unified_session.sql
-- Merge Telegram and webchat sessions into one canonical session key.
--
-- Before: telegram:7636850023 and agent:main:webchat-philip are separate sessions.
-- After:  agent:main:primary is the single canonical session for both channels.

BEGIN;

-- 1. Ensure the canonical session exists (create if not present)
INSERT INTO chat_sessions (tenant_id, session_key, channel, message_count)
VALUES ('robothor-primary', 'agent:main:primary', 'unified', 0)
ON CONFLICT (tenant_id, session_key) DO NOTHING;

-- 2. Move messages from the old Telegram session into the canonical session
INSERT INTO chat_messages (session_id, message, created_at)
SELECT
    (SELECT id FROM chat_sessions WHERE tenant_id = 'robothor-primary' AND session_key = 'agent:main:primary'),
    m.message,
    m.created_at
FROM chat_messages m
JOIN chat_sessions s ON m.session_id = s.id
WHERE s.tenant_id = 'robothor-primary'
  AND s.session_key = 'telegram:7636850023'
ORDER BY m.created_at;

-- 3. Move messages from the old webchat session into the canonical session
INSERT INTO chat_messages (session_id, message, created_at)
SELECT
    (SELECT id FROM chat_sessions WHERE tenant_id = 'robothor-primary' AND session_key = 'agent:main:primary'),
    m.message,
    m.created_at
FROM chat_messages m
JOIN chat_sessions s ON m.session_id = s.id
WHERE s.tenant_id = 'robothor-primary'
  AND s.session_key LIKE 'agent:main:webchat-%'
  -- Avoid duplicates if canonical session already had messages
  AND NOT EXISTS (
      SELECT 1 FROM chat_messages existing
      WHERE existing.session_id = (SELECT id FROM chat_sessions WHERE tenant_id = 'robothor-primary' AND session_key = 'agent:main:primary')
        AND existing.created_at = m.created_at
        AND existing.message = m.message
  )
ORDER BY m.created_at;

-- 4. Update message count on canonical session
UPDATE chat_sessions
SET message_count = (
    SELECT COUNT(*) FROM chat_messages
    WHERE session_id = (SELECT id FROM chat_sessions WHERE tenant_id = 'robothor-primary' AND session_key = 'agent:main:primary')
),
last_active_at = NOW()
WHERE tenant_id = 'robothor-primary'
  AND session_key = 'agent:main:primary';

-- 5. Delete old sessions (CASCADE deletes their messages too)
DELETE FROM chat_sessions
WHERE tenant_id = 'robothor-primary'
  AND session_key IN ('telegram:7636850023')
  AND session_key != 'agent:main:primary';

DELETE FROM chat_sessions
WHERE tenant_id = 'robothor-primary'
  AND session_key LIKE 'agent:main:webchat-%';

COMMIT;
