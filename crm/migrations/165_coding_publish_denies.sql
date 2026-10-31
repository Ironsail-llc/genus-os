-- Migration 165: cloning a repository for coding, and opening a pull request
-- from a coding job, are permissions, not just advertisements.
--
-- claude_code_repo_prepare clones an allowlisted remote into the coding workspace;
-- claude_code_publish pushes a verified coding job's branch to that remote and
-- opens a draft pull request as the instance's GitHub account.
--
-- So, as 146 and 157 do for their tools:
--
--   * 'user' and 'member' -- humans who are not the operator -- are denied
--     both, as they are claude_code_start (146).
--   * 'service' -- every unattended run -- is denied claude_code_publish: a pull
--     request leaves the host under the instance's name, and only a person
--     with the owner's or an admin's role opens one. It keeps
--     claude_code_repo_prepare (an unattended agent that opts in may refresh a
--     clone before a coding job).
--   * 'owner' and 'admin' keep their catch-all allow.
--
-- Most-specific pattern wins in check_tool_permission (exact beats '*'), so
-- these exact denies beat the roles' '*' allow. A tenant that wants a tool
-- back for a role adds a tenant-scoped row, evaluated before __default__.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['user', 'member']) AS role,
     unnest(ARRAY['claude_code_repo_prepare', 'claude_code_publish']) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
VALUES ('__default__', 'service', 'claude_code_publish', 'deny')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__'
--      AND role IN ('service', 'user', 'member')
--      AND tool_pattern IN ('claude_code_repo_prepare', 'claude_code_publish');
