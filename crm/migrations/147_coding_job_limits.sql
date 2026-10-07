-- Migration 147: per-job Claude Code limits on coding_jobs (robothor/engine/coding/jobs.py).
--
-- A job may set its own `--effort` level, `--max-turns` per round and round
-- timeout; NULL keeps the instance's coding settings (ROBOTHOR_CLAUDE_CODE_*).
-- The pr-reviewer's review jobs use them (ROBOTHOR_PR_REVIEW_EFFORT,
-- _MAX_TURNS, _ROUND_TIMEOUT). Stored on the row so a job resumed after an
-- engine restart keeps the limits it started with.
ALTER TABLE coding_jobs ADD COLUMN IF NOT EXISTS effort TEXT;
ALTER TABLE coding_jobs ADD COLUMN IF NOT EXISTS max_turns INTEGER;
ALTER TABLE coding_jobs ADD COLUMN IF NOT EXISTS round_timeout_s DOUBLE PRECISION;

-- Rollback:
--   ALTER TABLE coding_jobs DROP COLUMN IF EXISTS effort,
--       DROP COLUMN IF EXISTS max_turns, DROP COLUMN IF EXISTS round_timeout_s;
