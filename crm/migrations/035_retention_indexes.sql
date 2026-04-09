-- 035: Add indexes to support retention cleanup batch deletes.
-- These indexes make the time-based DELETE queries fast across all
-- tables managed by the retention policy (robothor/engine/retention.py).

-- Hot tier tables (30-day retention)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_run_steps_created
    ON agent_run_steps(created_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_run_checkpoints_created
    ON agent_run_checkpoints(created_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_guardrail_events_created
    ON agent_guardrail_events(created_at);

-- Warm tier tables (90-day retention)
-- audit_log: idx_audit_timestamp already exists
-- telemetry: idx_telemetry_timestamp already exists

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ingested_items_ingested_at
    ON ingested_items(ingested_at);

-- Federation events: partial index for synced-only cleanup
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fed_events_cleanup
    ON federation_events(created_at)
    WHERE synced_at IS NOT NULL;

-- autodream_runs
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_autodream_runs_started
    ON autodream_runs(started_at);
