-- Bounded resume attempts for runs interrupted by a restart.
--
-- On restart the daemon REAPS every run still marked 'running' — the work is
-- lost even though `CheckpointManager` has its messages and scratchpad on
-- disk and `_resume_from_checkpoint` can restore them. A competitive audit
-- found this the one durability axis where a SQLite-backed harness beats
-- this Postgres-backed platform: OpenClaw resumes in-flight runs with a
-- charged attempt budget, we do not.
--
-- The counter has to be durable, because the failure it guards against is a
-- crash LOOP: a run that dies during resume must not resume forever. An
-- in-memory count resets on exactly the event it exists to survive.
ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS resume_attempts INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN agent_runs.resume_attempts IS
    'Times this run has been resumed after an interrupting restart. Charged '
    'before the attempt, never after, so a crash during resume still counts.';
