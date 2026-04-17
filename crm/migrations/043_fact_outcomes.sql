-- Migration 043: Outcome-driven fact invalidation
--
-- Tracks which facts were consulted during each agent run (fact_access_log)
-- so that on a failed run we can attribute blame and bump outcome_failures
-- on the facts that were in the agent's context. Repeated failures drop
-- confidence and accelerate decay.

BEGIN;

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS outcome_failures INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_failure_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_facts_outcome_failures
    ON memory_facts(outcome_failures)
    WHERE outcome_failures > 0;

-- Per-retrieval audit — which fact ids were returned for which run.
CREATE TABLE IF NOT EXISTS fact_access_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent_id TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
        REFERENCES crm_tenants(id),
    fact_id INTEGER NOT NULL,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_access_run
    ON fact_access_log(run_id);
CREATE INDEX IF NOT EXISTS idx_fact_access_fact
    ON fact_access_log(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_access_time
    ON fact_access_log(accessed_at);

COMMIT;
