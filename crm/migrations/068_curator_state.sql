-- Rip 5: skill curator state.
--
-- Tracks the last consolidation pass per tenant so the daily
-- scheduler tick can decide whether to fire (default: every 7 days
-- with min_idle_hours guard). One row per tenant; updated in place.

CREATE TABLE IF NOT EXISTS crm_curator_state (
    tenant_id              TEXT PRIMARY KEY,
    last_pass_at           TIMESTAMPTZ,
    last_pass_dry_run      BOOLEAN NOT NULL DEFAULT TRUE,
    last_archived_count    INTEGER NOT NULL DEFAULT 0,
    last_merged_count      INTEGER NOT NULL DEFAULT 0,
    last_proposed_count    INTEGER NOT NULL DEFAULT 0,
    last_summary           TEXT,
    consecutive_dry_runs   INTEGER NOT NULL DEFAULT 0,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_curator_state_last_pass
    ON crm_curator_state(last_pass_at);
