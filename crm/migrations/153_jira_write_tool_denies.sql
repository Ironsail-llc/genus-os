-- Migration 153: the Jira write tools are permissions, not just
-- advertisements -- the RBAC half of their OPT_IN_TOOLS entry, under
-- migration 146's pattern.
--
-- jira_create_issue, jira_add_comment, jira_transition_issue and
-- jira_update_issue create, comment on, move and edit tickets as the
-- instance's own Jira account.
--
--   * 'user' and 'member' -- humans who are not the operator -- are denied all
--     four. Without this a team member chatting with an agent that opted in
--     could create, edit and move tickets as the instance's account.
--   * 'service' keeps its catch-all allow. Every unattended run dispatches as
--     'service' (cron, workflow steps, sub-agents, main's heartbeat), so a
--     deny would stop an agent whose manifest names the tools outright, and a
--     dedicated role cannot carve them back out. For unattended agents the
--     tools stay gated by OPT_IN_TOOLS plus the runner's tools_allowed guard:
--     only an agent whose manifest names them is offered them, and a call to
--     one it was not offered is refused.
--   * 'owner' and 'admin' keep their catch-all allow.
--
-- The Jira read tools are unchanged. Most-specific pattern wins in
-- check_tool_permission (exact beats '*'), so these exact denies beat the
-- roles' '*' allow. A tenant that wants a tool back for a role adds a
-- tenant-scoped row, evaluated before __default__.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['user', 'member']) AS role,
     unnest(ARRAY[
         'jira_create_issue', 'jira_add_comment', 'jira_transition_issue', 'jira_update_issue'
     ]) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__' AND role IN ('user', 'member')
--      AND tool_pattern IN ('jira_create_issue', 'jira_add_comment',
--                           'jira_transition_issue', 'jira_update_issue');
