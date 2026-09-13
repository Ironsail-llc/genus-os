-- Questions an agent asked a person, and what came back.
--
-- WHY NOT `workflow_approvals`. That table is the durable half of the SAME
-- idea and reusing it was the first thing considered. Two facts about it make
-- reuse a lie rather than a shortcut:
--
--   1. `WorkflowEngine.drive_approvals` calls `resume_run(row.run_id)` for
--      EVERY decided row it finds. An answered agent question would drive a
--      workflow resume sweep against an `agent_runs` id, which is not a
--      workflow run at all.
--   2. Its status CHECK is ('pending','approved','rejected','expired'). An
--      answer is free text, not a verdict. Storing "Tuesday, but move the 3pm"
--      in `decision_note` under status='approved' records a decision nobody
--      made.
--
-- So the shapes are siblings, not the same row. What they share -- the reason
-- either exists -- is that the question OUTLIVES the process that asked it.
-- `permission_escalation.py` keeps its pending prompts in an `asyncio.Event`
-- inside a dict: restart the engine and the question is gone with no row, no
-- log and no page. Here the ask is written down BEFORE the channel is called,
-- so a question nobody answered in time is still answerable afterwards (from
-- the Helm, from another surface, tomorrow) instead of being a fact that only
-- a dead coroutine ever knew.
--
-- `kind` distinguishes the two askers that write here: 'question' is the
-- `ask_user` tool, 'escalation' is a guardrail pausing a tool call. Same row
-- shape, different urgency, and an operator triaging a backlog needs to be
-- able to tell them apart.
--
-- NO `UNIQUE (run_id, ...)`. `workflow_approvals` has one because a resumed
-- run re-enters the same step and must find the same question. A run may
-- legitimately ask twice ("which one?" then "are you sure?"), and a
-- uniqueness constraint here would silently swallow the second.

CREATE TABLE IF NOT EXISTS agent_questions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    -- The agent_runs id that asked. Not an FK: the question must survive a
    -- retention sweep over run history, since "what did it ask, and what did
    -- we say" is the part with the longer useful life.
    run_id          UUID NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'question'
                    CHECK (kind IN ('question', 'escalation')),
    question        TEXT NOT NULL,
    -- The choices offered, when there were any. Empty array means free text.
    -- Snapshotted rather than re-derived: an answer of "the second one" is
    -- only readable against the options as they were WHEN ASKED.
    options         JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Where it was asked and at what address, for the operator reading a
    -- backlog. Empty when no channel could be reached at all, which is itself
    -- the interesting case.
    channel         TEXT NOT NULL DEFAULT '',
    target          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'answered', 'expired', 'cancelled')),
    answer          TEXT,
    answered_by     TEXT,
    answered_at     TIMESTAMPTZ,
    -- When the asking tool stopped waiting. Not "delete this": the row is
    -- stamped `expired` and KEPT, because "nobody answered" is exactly the
    -- fact an operator needs later and exactly the one a cleanup DELETE
    -- destroys.
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The two scans anything runs against this table: "what is still open" (the
-- Helm's list and the watchdog's expiry sweep) and "what did this run ask".
-- The first is partial because the table is mostly settled history.
CREATE INDEX IF NOT EXISTS idx_agent_questions_pending
    ON agent_questions (tenant_id, expires_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_agent_questions_run
    ON agent_questions (run_id);

-- Tenant isolation, in the permissive-when-unbound shape of 081: a connection
-- that never sets app.tenant_id (migrations, psql, the CLI) keeps working; one
-- that does is confined.
ALTER TABLE agent_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_questions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON agent_questions;
CREATE POLICY tenant_isolation ON agent_questions
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
