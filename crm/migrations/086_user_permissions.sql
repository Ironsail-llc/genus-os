-- Migration 086: Per-user permission overrides (Task 5, Unified Identity Context)
--
-- role_permissions (migration 037) is the only permission granularity today:
-- every user_accounts/tenant_users row of a given role shares the exact same
-- tool access. This adds a per-user override layer above it: an operator can
-- allow or deny a specific tool (or glob) for one specific user_id without
-- touching the role's defaults for everyone else.
--
-- user_id here is agent_runs.user_id semantics: user_accounts.id (TEXT cast)
-- for webchat, tenant_users.user_id for Telegram — both are TEXT, so a single
-- TEXT column fits either source without a FK (there is no single users table
-- spanning both channels yet).
--
-- Evaluation order (robothor/engine/permissions.py::check_tool_permission):
--   1. Most-specific matching row here (same fnmatch specificity metric as
--      role_permissions) — wins outright, allow or deny.
--   2. No match → falls through to the existing, unchanged role_permissions
--      evaluation.
--   3. No match anywhere → denied (fail-closed, unchanged).
--
-- Rollback: DROP TABLE IF EXISTS user_permissions;

CREATE TABLE IF NOT EXISTS user_permissions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    tool_pattern TEXT NOT NULL,           -- fnmatch glob: '*', 'delete_*', etc.
    access      TEXT NOT NULL CHECK (access IN ('allow', 'deny')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, tool_pattern)
);

CREATE INDEX IF NOT EXISTS idx_user_permissions_lookup
    ON user_permissions (tenant_id, user_id);
