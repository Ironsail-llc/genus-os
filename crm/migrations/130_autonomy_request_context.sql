-- Keep request attribution separate from the operation fingerprint: a new run
-- may retry the same idempotent operation without rewriting who first asked.
-- No FK to agent_runs: run retention must not delete this authorization record.
ALTER TABLE autonomy_operations ADD COLUMN IF NOT EXISTS request_context JSONB;
