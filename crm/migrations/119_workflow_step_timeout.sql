-- 119_workflow_step_timeout.sql
-- A workflow step may now be recorded as 'timeout'.
--
-- `_persist_step` only ever wrote a row when a step FINISHED, so a step that
-- hung for the whole workflow budget left no row at all: every timed-out
-- `email-pipeline` run in the 2026-09-13 diagnosis showed `steps 0/2` with
-- zero rows in `workflow_run_steps`, and "the workflow never started" was
-- indistinguishable from "step 1 has been running for fifteen minutes".
--
-- The engine now writes the row when the step STARTS, as 'running', and
-- updates it on completion. That leaves one new terminal state to record: the
-- run's own deadline fired while the step was still in flight. Without
-- 'timeout' in this CHECK the closing UPDATE fails and the row stays 'running'
-- forever -- the immortal-orphan shape the run-level finalizer already exists
-- to prevent, one table down.
ALTER TABLE workflow_run_steps DROP CONSTRAINT IF EXISTS workflow_run_steps_status_check;
ALTER TABLE workflow_run_steps ADD CONSTRAINT workflow_run_steps_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped',
                      'waiting', 'timeout'));
