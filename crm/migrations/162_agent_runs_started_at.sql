-- Migration 162: index agent_runs.started_at for the Runs page.
--
-- The Runs view (crm/bridge/routers/runs.py) filters agent_runs with
--     started_at >= NOW() - make_interval(hours => N)
-- and pages it with
--     ORDER BY started_at DESC NULLS LAST, id DESC LIMIT n OFFSET m
-- while its summary strip counts the same window. agent_runs had indexes on
-- created_at, (agent_id, status), tenant_id and correlation_id, none on
-- started_at, so every list page and every summary scanned the whole table
-- and sorted it. This index matches the sort exactly: the list reads its
-- first page straight off the index and the window becomes an index range.
--
-- NOTE: the runner executes each file in one transaction together with its
-- history row, and PostgreSQL refuses CREATE INDEX CONCURRENTLY inside a
-- transaction, so this file uses the plain form (a write lock on agent_runs
-- while it builds). On a large live table build it out of band first with
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_runs_started_at
--         ON agent_runs (started_at DESC NULLS LAST, id DESC);
-- and this migration then finds it and does nothing (same convention as
-- migrations 073, 074 and 109).

CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at
    ON agent_runs (started_at DESC NULLS LAST, id DESC);

-- Rollback:
--   DROP INDEX IF EXISTS idx_agent_runs_started_at;
