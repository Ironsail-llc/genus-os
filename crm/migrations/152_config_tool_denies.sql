-- Migration 152: the settings tools are the owner's (GEN-42).
--
--   * config_get reads the instance's effective settings and where each one
--     comes from. No secret value is returned, but the whole configuration of
--     the box is the operator's view.
--   * config_set changes those settings, and with apply=true rewrites systemd
--     drop-ins and restarts services through host exec.
--
-- Both handlers already refuse anyone but a verified owner identity
-- (robothor/engine/tools/handlers/config_tools.py). This is the RBAC half,
-- under migration 146's pattern: an exact pattern beats the roles' '*' allow,
-- so 'service', 'user', 'member' and 'admin' are denied both. 'owner' keeps
-- its catch-all allow; 'viewer' already holds a '*' deny. A tenant that wants
-- either back for a role adds a tenant-scoped row.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['service', 'user', 'member', 'admin']) AS role,
     unnest(ARRAY['config_get', 'config_set']) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__' AND role IN ('service', 'user', 'member', 'admin')
--      AND tool_pattern IN ('config_get', 'config_set');
