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
-- Semantics change — escalation_count:
--   Phase 1 of this work also redefines `crm_tasks.escalation_count`.
--   It used to be a lifetime count of every set_question() escalation.
--   It is now "consecutive escalations since the last operator
--   answer" — approve_task / reject_task reset it to 0. Downstream
--   dashboards and queries that historically summed or trended this
--   column should re-read it as current stall depth, not lifetime
--   tally. The migration records this intent via COMMENT ON COLUMN
--   below so future readers don't have to reverse-engineer it.
--
-- Rollback:
--   ALTER TABLE crm_task_history DROP CONSTRAINT IF EXISTS crm_task_history_metadata_kind_check;
--   ALTER TABLE crm_tasks DROP COLUMN IF EXISTS question_resolved_at;
--   ALTER TABLE crm_tasks DROP COLUMN IF EXISTS question_resolved_by;
--   COMMENT ON COLUMN crm_tasks.escalation_count IS NULL;
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE crm_tasks
    ADD COLUMN IF NOT EXISTS question_resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS question_resolved_by TEXT;

-- Document the kind enum on the column. Keeps psql \d+ readers pointed
-- at the canonical reference instead of having to grep the codebase.
COMMENT ON COLUMN crm_task_history.metadata IS
    'JSONB event payload. May include a "kind" discriminator drawn from the enum in docs/TASK_HISTORY_KIND.md.';

-- Document the semantics change on escalation_count. Pre-PR it was a
-- lifetime tally of set_question() calls. As of Phase 1, approve_task /
-- reject_task reset it to 0 — so any dashboard that historically read it
-- as "how many times has this task ever been escalated?" needs to re-read
-- it as "what's the current stall depth?". The COMMENT keeps the
-- redefinition visible to any future schema reader without grepping.
COMMENT ON COLUMN crm_tasks.escalation_count IS
    'Count of consecutive set_question() escalations since the last operator answer. Reset to 0 by approve_task / reject_task — this is current stall depth, not lifetime tally.';

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
