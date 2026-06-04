-- Migration 038: Add reap_category to agent_runs
--
-- The daemon watchdog reaps any agent_runs row still status='running' after
-- 30 minutes. Previously, every reaped row got the hardcoded error_message
-- "Reaped by watchdog: stuck in initialization (no LLM call reached)" —
-- a misleading label applied without actually checking whether the LLM was
-- called. This column captures the real classification so observability
-- queries can group by cause instead of parsing text.
--
-- Values (set by daemon._cleanup_stale_runs):
--   'no_steps'         — no agent_run_steps rows for this run (likely crash
--                         during setup / before first LLM call)
--   'post_llm_crash'   — LLM was called at least once; runner died after
--   'post_tool_crash'  — last recorded step was a tool_call/tool_result
--   'post_error_crash' — last recorded step was an 'error' step
--   'daemon_restart'   — run started before the current daemon boot timestamp
--                         (systemd stopped the old daemon mid-run)
--   NULL               — row was not reaped (completed/failed/timeout via
--                         runner-side path)
--
-- Nullable and unindexed — this is a diagnostic column, not a query driver.
-- Existing rows remain NULL; only future reaper events populate it.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS reap_category text;

COMMENT ON COLUMN agent_runs.reap_category IS
    'Set by daemon reaper when status=timeout. One of: no_steps, post_llm_crash, post_tool_crash, post_error_crash, daemon_restart. NULL for non-reaped timeouts.';
