-- ─────────────────────────────────────────────────────────────────────────────
-- 067_task_history_kind_schema.sql
--
-- Phase 1 of the task-system stabilization. Locks down the data contract
-- for `crm_task_history.metadata` and adds the "question resolved" columns
-- on `crm_tasks` that the Phase-4 operator-answer endpoint reads.
--
-- Background:
--   `crm_task_history` rows carry a JSONB `metadata` field, and by
--   convention that object includes a `kind` discriminator. The forward
--   planner (thread_planner.py) reads `kind` to infer what happened last
--   on a thread (email_sent? calendar_offer_received? ask? plan?). Until
--   now the set of valid kinds existed only as scattered string literals.
--
--   docs/TASK_HISTORY_KIND.md is the single source of truth; this
--   migration encodes the same set as a Postgres CHECK constraint on
--   future rows. The constraint ships as `NOT VALID` so legacy rows
--   carrying older kind values aren't rejected on upgrade — a follow-up
--   migration runs `VALIDATE CONSTRAINT` once an audit confirms the
--   existing rows are conformant.
--
-- New columns on crm_tasks:
--   question_resolved_at   — TIMESTAMPTZ, set when an operator answers
--                            via dal.answer_question (Phase 4). NULL
--                            until then. Lets dashboards report
--                            answer latency without joining history.
--   question_resolved_by   — TEXT, the agent_id of the answerer
--                            (typically "helm-user").
--
-- Rollback:
--   ALTER TABLE crm_task_history DROP CONSTRAINT IF EXISTS crm_task_history_metadata_kind_check;
--   ALTER TABLE crm_tasks DROP COLUMN IF EXISTS question_resolved_at;
--   ALTER TABLE crm_tasks DROP COLUMN IF EXISTS question_resolved_by;
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE crm_tasks
    ADD COLUMN IF NOT EXISTS question_resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS question_resolved_by TEXT;

-- Document the kind enum on the column. Keeps psql \d+ readers pointed
-- at the canonical reference instead of having to grep the codebase.
COMMENT ON COLUMN crm_task_history.metadata IS
    'JSONB event payload. May include a "kind" discriminator drawn from the enum in docs/TASK_HISTORY_KIND.md.';

-- CHECK constraint on future rows: when `metadata->>'kind'` is present, it
-- must be one of the documented values. NOT VALID skips existing rows on
-- create (we ship a follow-up migration once an audit confirms legacy
-- rows match). New rows are validated immediately.
ALTER TABLE crm_task_history
    DROP CONSTRAINT IF EXISTS crm_task_history_metadata_kind_check;

ALTER TABLE crm_task_history
    ADD CONSTRAINT crm_task_history_metadata_kind_check
    CHECK (
        metadata IS NULL
        OR NOT (metadata ? 'kind')
        OR metadata->>'kind' IN (
            'plan',
            'ask',
            'answer',
            'email_sent',
            'calendar_offer_received',
            'todo_promoted',
            'acceptance'
        )
    )
    NOT VALID;
