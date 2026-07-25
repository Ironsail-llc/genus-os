-- Migration 092: Durable roll-up for fact access history
--
-- WHY
--   fact_access_log (043) is the only signal the memory system has for "was
--   this fact ever actually useful". cleanup_old_access_logs hard-DELETEs rows
--   past a retention window every night, so that signal was being destroyed on
--   a rolling basis: at audit time the primary tenant retained only 23 days of
--   history, and memory_facts.access_count was non-zero on 21 of ~153k rows
--   because the source data kept evaporating before anything read it.
--
--   The decay formula (robothor/memory/lifecycle.py compute_decay_score) needs
--   a lifetime access count, not a 30-day one. This table is that lifetime
--   aggregate. The raw log stays GC-able — it is per-run audit detail and is
--   genuinely not needed forever — but the counts survive.
--
-- HOW
--   cleanup_old_access_logs folds the rows it is about to delete into this
--   table in the SAME statement (a DELETE ... RETURNING feeding an upsert), so
--   there is no window in which rows are deleted but not yet accounted for.
--   ON CONFLICT accumulates rather than overwrites — successive nightly passes
--   add to the count instead of clobbering the previous night's.
--
-- SCOPE
--   Additive. No existing table is modified. No backfill: history already
--   deleted before this migration is unrecoverable, so the rollup starts from
--   whatever remains in fact_access_log at first sweep.
--
-- IDEMPOTENT
--   CREATE TABLE / INDEX / POLICY are all IF NOT EXISTS or DROP-then-CREATE.
--
-- Rollback: DROP TABLE IF EXISTS fact_access_rollup;

BEGIN;

CREATE TABLE IF NOT EXISTS fact_access_rollup (
    fact_id           INTEGER     NOT NULL,
    tenant_id         TEXT        NOT NULL DEFAULT 'default'
                                  REFERENCES crm_tenants(id),
    access_count      BIGINT      NOT NULL DEFAULT 0,
    first_accessed_at TIMESTAMPTZ,
    last_accessed_at  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fact_id, tenant_id)
);

-- Lookup pattern is "recently-consulted facts for this tenant", used by the
-- decay backfill and by any future usefulness reporting.
CREATE INDEX IF NOT EXISTS idx_access_rollup_tenant
    ON fact_access_rollup(tenant_id, last_accessed_at DESC);

-- Migration 081 applied RLS by looping over tables that already had a
-- tenant_id column; it does not cover tables created afterwards. A new
-- tenant-scoped table must carry its own policy or it silently sits outside
-- the isolation backstop.
ALTER TABLE fact_access_rollup ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_access_rollup FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON fact_access_rollup;
CREATE POLICY tenant_isolation ON fact_access_rollup
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
