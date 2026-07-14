-- 006_task_state_machine.sql
-- Adds: REVIEW status, transition history table, SLA tracking columns.

-- 1. CHECK constraint on valid statuses
-- The canonical fresh-install chain applies this after the consolidated
-- 001_init snapshot, which may already contain the equivalent objects.
ALTER TABLE crm_tasks DROP CONSTRAINT IF EXISTS valid_status;
ALTER TABLE crm_tasks ADD CONSTRAINT valid_status
    CHECK (status IN ('TODO', 'IN_PROGRESS', 'REVIEW', 'DONE'));

-- 2. Task transition history (append-only audit trail)
CREATE TABLE IF NOT EXISTS crm_task_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES crm_tasks(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_by TEXT,
    reason TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_history_task ON crm_task_history (task_id);
CREATE INDEX IF NOT EXISTS idx_task_history_created ON crm_task_history (created_at);

-- 3. SLA tracking
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS sla_deadline_at TIMESTAMPTZ;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS escalation_count INT DEFAULT 0;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
