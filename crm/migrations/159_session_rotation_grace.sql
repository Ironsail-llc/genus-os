-- Migration 159: refresh rotation that a second dashboard replica can replay.
--
-- Refresh tokens are single-use. With more than one dashboard replica, two
-- of them can present the same token a few milliseconds apart; the second
-- used to be refused. A rotated row now records WHEN it was rotated and the
-- session it was rotated TO, so a replay inside the short grace window
-- (robothor.auth.tokens.REFRESH_REUSE_GRACE_SECONDS) is answered with the
-- same successor. A row revoked by sign-out or an operator has rotated_at
-- NULL and is never replayable. Only hashes are stored, as before.
ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMPTZ;
ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS rotated_to UUID
    REFERENCES user_sessions(id) ON DELETE SET NULL;

-- Rollback:
--   ALTER TABLE user_sessions DROP COLUMN IF EXISTS rotated_to;
--   ALTER TABLE user_sessions DROP COLUMN IF EXISTS rotated_at;
