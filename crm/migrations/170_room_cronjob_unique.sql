-- Migration 170: one schedule row per room in user_cronjobs.
--
-- A scheduled workspace keeps exactly one user_cronjobs row, keyed by room_id
-- (migration 169 added the column with a plain index). The sync that writes it
-- (robothor/engine/user_cron.py, sync_room_cronjob) is a single
-- INSERT ... ON CONFLICT against this partial unique index, so two concurrent
-- syncs of a room with no row yet cannot leave two rows behind. Plain user
-- crons carry no room_id and are untouched.
--
-- Idempotent: a room that somehow already has more than one row keeps its
-- oldest (the one a scheduler would have seen first) before the index is built,
-- and the index is created only if missing.
DELETE FROM user_cronjobs u
USING user_cronjobs keep
WHERE u.room_id IS NOT NULL
  AND keep.room_id = u.room_id
  AND keep.tenant_id = u.tenant_id
  AND (keep.created_at, keep.job_id) < (u.created_at, u.job_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_cronjobs_room
    ON user_cronjobs (tenant_id, room_id) WHERE room_id IS NOT NULL;

-- Rollback:
--   DROP INDEX IF EXISTS uq_user_cronjobs_room;
