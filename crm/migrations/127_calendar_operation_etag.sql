-- Write-ahead evidence for an interrupted attendee write.
--
-- The executing marker alone can only say "outcome unknown", and an unknown
-- outcome arms a barrier that refuses every later draft and every direct
-- write for that meeting. Recording the event version observed immediately
-- BEFORE the request makes the common case provable: an unchanged etag with
-- none of the requested attendees present means the write never landed, and
-- the barrier clears itself instead of freezing the meeting forever.
BEGIN;
ALTER TABLE calendar_operations ADD COLUMN IF NOT EXISTS pre_write_etag text;
COMMIT;
