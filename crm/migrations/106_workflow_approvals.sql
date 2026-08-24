-- Durable human-in-the-loop approvals for workflow steps.
--
-- WHY. The engine already has a human-approval path for tool calls
-- (permission_escalation.py), and it lives entirely in RAM: an
-- `asyncio.Event` in a dict, a Telegram inline keyboard, and a 300-second
-- timeout that DENIES. That shape is right for "the agent is mid-run and the
-- operator is at the keyboard". It is wrong for a workflow that wants to ask
-- "send this to the board?" at 03:00 and wait until morning — the process
-- restarts, the dict is empty, and the pending question is simply gone. No
-- row, no log, no page: the workflow reports a step failure whose real cause
-- ("nobody was asked") is unrecoverable from stored data.
--
-- So an approval becomes a ROW. The run suspends, the row outlives the
-- process, and the decision — including "nobody decided in time" — is a fact
-- the operator can audit later.
--
-- WHAT `expires_at` MEANS. Not "delete this". It is the moment the step's
-- declared `on_timeout` policy applies, and the row is then stamped
-- `expired` and KEPT. A silent auto-approve and a silent auto-abort are
-- equally unacceptable if neither leaves evidence of which one happened.
--
-- The UNIQUE (run_id, step_id) is the resume interlock: a resumed run
-- re-enters the same step and must find the SAME question, not ask a second
-- one. Without it a restart loop would page the operator once per restart.

CREATE TABLE IF NOT EXISTS workflow_approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    run_id          UUID NOT NULL,
    workflow_id     TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    -- What the operator needs in order to decide, rendered at ask time.
    -- Snapshotted rather than re-derived on read: the run's context keeps
    -- moving after the question is asked, and an approval shown against
    -- later state is a different question than the one that was asked.
    detail          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, step_id)
);

-- The two queries the resume driver runs every tick: "what has been decided"
-- and "what has run out of time". Both are pending-or-decided scans over a
-- table that is mostly settled history, so both are partial.
CREATE INDEX IF NOT EXISTS idx_workflow_approvals_pending
    ON workflow_approvals (tenant_id, expires_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_workflow_approvals_decided
    ON workflow_approvals (tenant_id, decided_at)
    WHERE status IN ('approved', 'rejected');

CREATE INDEX IF NOT EXISTS idx_workflow_approvals_run
    ON workflow_approvals (run_id);

-- Tenant isolation, in the permissive-when-unbound shape of 081: a
-- connection that never sets app.tenant_id (migrations, psql, the CLI) keeps
-- working; one that does is confined.
ALTER TABLE workflow_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_approvals FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON workflow_approvals;
CREATE POLICY tenant_isolation ON workflow_approvals
    USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    )
    WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    );

-- A suspended run is not running, and it is emphatically not failed. Without
-- its own status the CHECK constraint would force a lie into the ledger, and
-- every "how healthy is the fleet" query that counts non-completed runs as
-- problems would count patient waiting as breakage.
ALTER TABLE workflow_runs DROP CONSTRAINT IF EXISTS workflow_runs_status_check;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'failed',
                      'timeout', 'cancelled', 'skipped', 'awaiting_approval'));

-- The step-level constraints have been out of date since 013. `parallel`
-- shipped as a step type without ever being added here, so `_persist_step`
-- has been raising on every parallel step and swallowing it into a
-- `logger.warning` — a feature that is built, tested, documented, and
-- invisible in the ledger. No workflow declares one yet, so nothing has been
-- lost; the first one to try would have lost every branch row silently.
ALTER TABLE workflow_run_steps DROP CONSTRAINT IF EXISTS workflow_run_steps_step_type_check;
ALTER TABLE workflow_run_steps ADD CONSTRAINT workflow_run_steps_step_type_check
    CHECK (step_type IN ('agent', 'tool', 'condition', 'transform',
                         'noop', 'parallel', 'approval'));

-- `waiting` is the step-level mirror of `awaiting_approval`: the step has
-- been reached and asked, and is neither running nor finished.
ALTER TABLE workflow_run_steps DROP CONSTRAINT IF EXISTS workflow_run_steps_status_check;
ALTER TABLE workflow_run_steps ADD CONSTRAINT workflow_run_steps_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'waiting'));

-- RunStatus is one enum serving two tables. `awaiting_approval` is reachable
-- only for workflow runs today, but the constraint gate (test_schema_drift)
-- checks the whole enum against agent_runs precisely because a shared enum
-- means any member can reach any of its tables through a future code path —
-- and a status the CHECK rejects fails at terminal-update time, which is the
-- moment a run is trying to record what happened to it.
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'failed',
                      'timeout', 'cancelled', 'skipped', 'awaiting_approval'));
