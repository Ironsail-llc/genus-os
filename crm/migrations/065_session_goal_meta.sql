-- ─────────────────────────────────────────────────────────────────────────────
-- 065_session_goal_meta.sql
--
-- Backs the session-goal feature with a crm_task row instead of brain/GOAL.md.
-- The session goal is a long-running operator objective; before this migration
-- it lived in a markdown file and was auto-injected into every agent's system
-- prompt, with a substring-scan completion guard.
--
-- A session goal is now a crm_task with:
--   * tag 'session_goal'
--   * (optional) tag 'agent:<agent_id>' for agent-scoped goals (else workspace-scoped)
--   * objective    column carries the operator's stated objective
--   * status       TODO = active, DONE = complete (uses existing CRM enum)
--   * session_goal_meta JSONB carries the structured payload that doesn't
--     map onto existing crm_task columns.
--
-- session_goal_meta shape:
--   {
--     "success_criteria": ["…", "…"],
--     "evidence": [
--       {"kind": "test_run|commit|ci_run|note",
--        "summary": "…",
--        "reference": "pytest:passed:42 | <git-sha> | https://… | <free>",
--        "recorded_at": "2026-05-09T…+00:00",
--        "valid": true|false}
--     ],
--     "completion_note": "…"
--   }
--
-- The partial GIN index makes the singleton lookup (get_active_session_goal)
-- O(log n) — there is exactly one active session goal per (tenant, agent)
-- in steady state.
--
-- Rollback:
--   ALTER TABLE crm_tasks DROP COLUMN session_goal_meta;
--   DROP INDEX idx_crm_tasks_session_goal;
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE crm_tasks
    ADD COLUMN IF NOT EXISTS session_goal_meta JSONB;

CREATE INDEX IF NOT EXISTS idx_crm_tasks_session_goal
    ON crm_tasks USING GIN (tags)
    WHERE deleted_at IS NULL AND 'session_goal' = ANY(tags);
