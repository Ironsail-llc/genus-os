-- Migration 017: Revert timezone from America/Grenada back to America/New_York
-- Philip returned from Grenada — all schedules should use Eastern Time.

ALTER TABLE agent_schedules ALTER COLUMN timezone SET DEFAULT 'America/New_York';
UPDATE agent_schedules SET timezone = 'America/New_York' WHERE timezone = 'America/Grenada';
