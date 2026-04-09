-- DevOps metrics snapshot storage for the Dev Team Operations Manager agent.
-- Stores periodic metric snapshots from JIRA, GitHub, and Claude Teams
-- for trend analysis and week-over-week comparison.

CREATE TABLE IF NOT EXISTS devops_metrics_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default',
    snapshot_date DATE NOT NULL,
    source TEXT NOT NULL,              -- 'jira', 'github', 'claude_teams'
    metric_type TEXT NOT NULL,         -- 'sprint_velocity', 'pr_cycle_time', 'review_turnaround', etc.
    scope TEXT NOT NULL DEFAULT 'team', -- 'team' or specific project/repo
    scope_key TEXT NOT NULL DEFAULT '', -- e.g., repo name, board name
    value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(tenant_id, snapshot_date, source, metric_type, scope, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_devops_metrics_date
    ON devops_metrics_snapshots (snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_devops_metrics_source
    ON devops_metrics_snapshots (source, metric_type);

CREATE INDEX IF NOT EXISTS idx_devops_metrics_scope
    ON devops_metrics_snapshots (scope, scope_key);
