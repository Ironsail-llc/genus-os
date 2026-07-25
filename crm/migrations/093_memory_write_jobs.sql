-- Migration 093: Durable queue for deferred memory writes
--
-- WHY
--   store_memory extracts facts with an LLM on the request path. Measured with
--   the model warm that is ~23s; in production the p50 is 63.5s because the
--   32B generation model is almost never resident (about 4 calls/day against a
--   5-minute keep_alive), and 15 of 121 calls over 30 days were killed at the
--   120s tool wall. The agents wrote themselves a fallback skill because of it.
--
--   Moving extraction off the request path fixes the latency, but introduces a
--   worse failure: a write the agent was told had succeeded, silently lost.
--   robothor/engine/task_registry.py is the right executor — the codebase
--   already defers memory writes through it from chat.py and telegram.py — but
--   daemon.py drains it with a 10-second budget against a ~60-second job under
--   Restart=always, so every deploy would destroy in-flight work.
--
--   This table is the durability. It answers the only question that matters
--   after a crash: was a write promised and not completed?
--
-- WHY NOT A REDIS STREAM
--   The existing consumer pattern (robothor/engine/hooks.py) creates groups at
--   '$' and reads '>' only, never re-reading its own pending list, so it has no
--   crash recovery to inherit. And bus.publish() returns None on failure
--   without raising, which would drop the memory while reporting success. A row
--   in the same database as the facts needs no second delivery semantics and no
--   Redis-outage branch.
--
-- SAFETY
--   Re-processing a job is harmless: MEMORY_WRITE_DEDUP is live and migration
--   078 gives a partial unique index on active (tenant_id, content_hash), so a
--   duplicate extraction produces no duplicate rows.
--
-- IDEMPOTENT: CREATE TABLE / INDEX / POLICY are IF NOT EXISTS or DROP-then-CREATE.
-- Rollback: DROP TABLE IF EXISTS memory_write_jobs;

BEGIN;

CREATE TABLE IF NOT EXISTS memory_write_jobs (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT        NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    content       TEXT        NOT NULL,
    content_type  TEXT        NOT NULL DEFAULT 'conversation',
    agent_id      TEXT,
    run_id        TEXT,
    -- pending -> running -> done | failed
    status        TEXT        NOT NULL DEFAULT 'pending',
    attempts      INTEGER     NOT NULL DEFAULT 0,
    fact_ids      INTEGER[],
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The sweeper's query: anything not finished, oldest first.
CREATE INDEX IF NOT EXISTS idx_write_jobs_unfinished
    ON memory_write_jobs(status, updated_at)
    WHERE status IN ('pending', 'running');

-- Operator/debug lookup by tenant.
CREATE INDEX IF NOT EXISTS idx_write_jobs_tenant
    ON memory_write_jobs(tenant_id, created_at DESC);

-- Migration 081 applied RLS by looping over tables that already carried a
-- tenant_id; it does not cover tables created afterwards, so this one brings
-- its own policy rather than sitting outside the isolation backstop.
ALTER TABLE memory_write_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_write_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON memory_write_jobs;
CREATE POLICY tenant_isolation ON memory_write_jobs
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

COMMIT;
