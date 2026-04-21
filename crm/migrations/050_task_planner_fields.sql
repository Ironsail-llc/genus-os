-- ─────────────────────────────────────────────────────────────────────────────
-- 050_task_planner_fields.sql
--
-- Stage 4 of the thread-pool work. Gives every task a structured plan so
-- stalls trigger the planner instead of a bare flag flip, and escalations
-- carry a concrete question instead of a boolean.
--
-- Columns:
--   objective             — the goal the thread is trying to accomplish, stable
--                           across follow-ups (e.g. "confirm RxHistory pricing
--                           without scheduling a meeting"). Workers that receive
--                           this task see objective alongside the thread body.
--   next_action           — the single next step the thread-level planner has
--                           chosen. Free-text hint consumed by the spawned
--                           worker's prompt (e.g. "email April asking for a
--                           written quote by EOW").
--   next_action_agent     — which agent should execute next_action. Planner
--                           picks one of the existing agent IDs.
--   blockers              — list of structured blockers. Shape is intentionally
--                           open (JSONB) until we know what patterns repeat.
--   question_for_operator — when the planner decides it has to ask, the
--                           specific question goes here. Replaces the bare
--                           requires_human=true signal as the handoff payload.
--   autonomy_budget       — per-task override of the tenant autonomy defaults.
--                           JSON shape keys: reversible_cap_usd,
--                           irreversible_cap_usd, categories (map of
--                           action_type → "auto"|"ask"|"refuse"), hard_floor.
--   last_planned_at       — timestamp of the most recent plan_thread() run.
--                           Skip re-planning if <4h old.
--   planner_version       — bumped when the planner's logic changes so stale
--                           plans get re-evaluated. 0 = never planned (lazy
--                           backfill for the 47 existing rows).
--
-- Rollback:
--   ALTER TABLE crm_tasks DROP COLUMN objective, DROP COLUMN next_action,
--       DROP COLUMN next_action_agent, DROP COLUMN blockers,
--       DROP COLUMN question_for_operator, DROP COLUMN autonomy_budget,
--       DROP COLUMN last_planned_at, DROP COLUMN planner_version;
--   DROP INDEX idx_crm_tasks_needs_planning;
--   DROP INDEX idx_crm_tasks_has_question;
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE crm_tasks
    ADD COLUMN IF NOT EXISTS objective             TEXT,
    ADD COLUMN IF NOT EXISTS next_action           TEXT,
    ADD COLUMN IF NOT EXISTS next_action_agent     TEXT,
    ADD COLUMN IF NOT EXISTS blockers              JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS question_for_operator TEXT,
    ADD COLUMN IF NOT EXISTS autonomy_budget       JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_planned_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS planner_version       SMALLINT NOT NULL DEFAULT 0;

-- Planner driver query: "give me the stalled threads that need a fresh plan"
CREATE INDEX IF NOT EXISTS idx_crm_tasks_needs_planning
    ON crm_tasks (last_planned_at)
    WHERE deleted_at IS NULL AND status NOT IN ('DONE', 'CANCELED');

-- Phase 3 "Need You" query: "tasks waiting on the operator with a real question"
CREATE INDEX IF NOT EXISTS idx_crm_tasks_has_question
    ON crm_tasks (tenant_id, updated_at DESC)
    WHERE question_for_operator IS NOT NULL AND question_for_operator <> ''
          AND deleted_at IS NULL;
