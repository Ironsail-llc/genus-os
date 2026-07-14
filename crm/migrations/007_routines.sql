-- 007_routines.sql
-- Recurring task templates with cron scheduling.

CREATE TABLE IF NOT EXISTS crm_routines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    body TEXT,
    cron_expr TEXT NOT NULL,
    timezone TEXT DEFAULT 'America/New_York',
    assigned_to_agent TEXT,
    priority TEXT DEFAULT 'normal',
    tags TEXT[] DEFAULT '{}',
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    active BOOLEAN DEFAULT TRUE,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_routines_due ON crm_routines (next_run_at)
    WHERE active = TRUE AND deleted_at IS NULL;
