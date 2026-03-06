-- Migration 024: Add requires_human flag to crm_tasks
-- Tasks with requires_human=TRUE cannot be auto-resolved by agents or cleanup crons.
-- Only Philip (via Helm) or explicit human action can resolve them.

ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS requires_human BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_crm_tasks_requires_human
    ON crm_tasks (requires_human) WHERE requires_human = TRUE;
