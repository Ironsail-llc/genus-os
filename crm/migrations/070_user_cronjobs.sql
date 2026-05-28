-- Rip 8: user-authored cron jobs.
--
-- Backs the natural-language `cronjob` tool — operator (or agent on
-- the operator's behalf) translates "remind me at 3 PM tomorrow"
-- into a `parse_schedule()` payload and persists a row here. The
-- scheduler reads this table on startup and on every reconcile pass
-- to wire jobs into APScheduler alongside the existing manifest
-- cron entries.

CREATE TABLE IF NOT EXISTS user_cronjobs (
    job_id              TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    created_by_session  TEXT,
    agent_id            TEXT NOT NULL,
    schedule_kind       TEXT NOT NULL,          -- 'once' | 'interval' | 'cron'
    schedule_payload    JSONB NOT NULL,         -- parsed dict from cron_parse.parse_schedule
    prompt              TEXT NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at         TIMESTAMPTZ,
    next_run_at         TIMESTAMPTZ,
    fire_count          INTEGER NOT NULL DEFAULT 0,
    max_fires           INTEGER,                -- NULL = unbounded; tool sets sensible default
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_cronjobs_next
    ON user_cronjobs(enabled, next_run_at)
    WHERE enabled = TRUE;

CREATE INDEX IF NOT EXISTS idx_user_cronjobs_tenant
    ON user_cronjobs(tenant_id, enabled);

CREATE INDEX IF NOT EXISTS idx_user_cronjobs_agent
    ON user_cronjobs(agent_id, enabled);
