-- ─────────────────────────────────────────────────────────────────────────────
-- 051_task_followup.sql
--
-- First-class long-running follow-ups. Tasks can be "snoozed" until a specific
-- wall-clock time; before that time they are filtered out of the thread pool
-- and drain queue. When the time passes, resurface_due_followups() (Python
-- DAL) clears follow_up_at, records a history row, and the task flows back
-- into the normal queues.
--
-- Design: we keep the existing status enum (TODO/IN_PROGRESS/REVIEW/DONE) and
-- gate visibility with follow_up_at. A task with `follow_up_at > NOW()` is
-- effectively hidden; a task with `follow_up_at IS NULL OR <= NOW()` shows up.
-- This avoids adding a new status without an upgrade path for every caller.
--
-- Filed by scout or drain; consumed by scout, drain, and list_tasks.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE crm_tasks
    ADD COLUMN IF NOT EXISTS follow_up_at timestamptz NULL;

-- Partial index: only "live" tasks that are actively snoozing. Keeps the
-- resurface scan O(rows-due-soon) instead of O(all-tasks).
CREATE INDEX IF NOT EXISTS idx_crm_tasks_followup_due
    ON crm_tasks (follow_up_at)
    WHERE status IN ('TODO', 'IN_PROGRESS', 'REVIEW')
      AND follow_up_at IS NOT NULL
      AND deleted_at IS NULL;
