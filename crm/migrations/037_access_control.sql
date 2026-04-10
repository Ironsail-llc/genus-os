-- Migration 037: Role-based access control
--
-- Adds the role_permissions table for per-tenant, per-role tool access
-- control.  Also adds child_data_access flag to crm_tenants for
-- hierarchical read access, and a stable user_id to tenant_users.

-- ── Role permissions table ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS role_permissions (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,              -- '__default__' for platform-wide defaults
    role TEXT NOT NULL,                    -- 'viewer', 'user', 'admin', 'owner'
    tool_pattern TEXT NOT NULL,            -- fnmatch glob: '*', 'search_*', etc.
    access TEXT NOT NULL DEFAULT 'allow',  -- 'allow' or 'deny'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, role, tool_pattern)
);

CREATE INDEX IF NOT EXISTS idx_role_permissions_lookup
    ON role_permissions (role, tenant_id);

-- ── Seed platform defaults ──────────────────────────────────────────
-- viewer: read-only tools
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'viewer', 'search_*', 'allow'),
    ('__default__', 'viewer', 'get_*', 'allow'),
    ('__default__', 'viewer', 'list_*', 'allow'),
    ('__default__', 'viewer', 'memory_block_read', 'allow'),
    ('__default__', 'viewer', 'memory_block_list', 'allow'),
    ('__default__', 'viewer', '*', 'deny'),
    -- user/admin/owner: full access
    ('__default__', 'user', '*', 'allow'),
    ('__default__', 'admin', '*', 'allow'),
    ('__default__', 'owner', '*', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- ── Hierarchical tenant access flag ─────────────────────────────────
-- When TRUE, owner/admin users in this tenant can read child tenant data.
ALTER TABLE crm_tenants
    ADD COLUMN IF NOT EXISTS child_data_access BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Stable user_id on tenant_users ──────────────────────────────────
-- The SERIAL id is internal.  user_id is the external stable identifier
-- used in audit trails and permission checks.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_users' AND column_name = 'user_id'
    ) THEN
        ALTER TABLE tenant_users ADD COLUMN user_id TEXT;
        -- Backfill existing rows
        UPDATE tenant_users SET user_id = id::TEXT WHERE user_id IS NULL;
        -- Make NOT NULL after backfill
        ALTER TABLE tenant_users ALTER COLUMN user_id SET NOT NULL;
        ALTER TABLE tenant_users ALTER COLUMN user_id SET DEFAULT gen_random_uuid()::TEXT;
        -- Add unique constraint
        ALTER TABLE tenant_users ADD CONSTRAINT tenant_users_user_id_key UNIQUE (user_id);
    END IF;
END $$;

-- ── Add user_id to agent_runs for audit trail ───────────────────────
ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT '';

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS user_role TEXT DEFAULT '';
