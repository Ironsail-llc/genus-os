-- Migration 157: run_workflow is a permission, not just an advertisement.
--
-- run_workflow starts any declared workflow on the instance, and a workflow
-- can do anything its steps do. The HTTP route it mirrors
-- (POST /api/workflows/{id}/execute) needs engine:control; without this row a
-- 'user'- or 'member'-role chat caller reached the same thing through main,
-- because the roles' default allow (migration 037) covered it.
--
-- So, as 146 and 153 do for their write tools:
--
--   * 'user' and 'member' -- humans who are not the operator -- are denied it.
--   * 'service' keeps its catch-all: an unattended agent that names the tool
--     in its manifest dispatches as 'service', and OPT_IN_TOOLS plus the
--     runner's tools_allowed guard decide which agents those are.
--   * 'owner' and 'admin' keep their catch-all allow.
--   * list_workflows and workflow_runs are reads and stay open to everyone.
--
-- Most-specific pattern wins in check_tool_permission (exact beats '*'), so
-- this exact deny beats the roles' '*' allow. A tenant that wants the tool
-- back for a role adds a tenant-scoped row, evaluated before __default__.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, 'run_workflow', 'deny'
FROM unnest(ARRAY['user', 'member']) AS role
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__'
--      AND role IN ('user', 'member')
--      AND tool_pattern = 'run_workflow';
