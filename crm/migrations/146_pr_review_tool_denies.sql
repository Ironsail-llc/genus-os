-- Migration 146: the review-posting, Claude Code and pr_review_* tools are
-- permissions, not just advertisements (the deferred RBAC half of #636/#637/#638).
--
-- Who runs these tools, and as which role (robothor/engine/runtime/setup.py
-- `principal`, robothor/engine/workflow.py, robothor/engine/scheduler.py):
--
--   * An interactive caller (Telegram, webchat, Slack, ...) dispatches as the
--     human's own role: owner/admin for the operator, user/member for anyone
--     else the instance lets talk to an agent.
--   * EVERY unattended run dispatches as 'service': the scheduler passes
--     user_role='service' for cron workflows, a workflow's agent step inherits
--     the workflow run's role, and a cron agent run falls back to it. The
--     pr-review-intake workflow's tool step, the pr-review-run workflow's
--     pr-reviewer agent step and main's heartbeat are all 'service' at the
--     dispatch gate -- a manifest `role:` does not change that for a
--     workflow-triggered run (it is checked in addition, by the system-run
--     gate in tool_admission.py, not instead).
--
-- So:
--
--   * 'user' and 'member' -- humans who are not the operator -- are denied all
--     nine. Without this a team member chatting with main could make it run
--     Claude Code on the host (main opts into claude_code_*), or post, reply
--     on and resolve GitHub reviews as the instance's account.
--   * 'service' is denied only the three github_* review-write tools. Nothing
--     unattended needs them: pr_review_finalize posts through their handlers
--     directly (not through dispatch), and the pr-reviewer template no longer
--     opts into them. Denying 'service' claude_code_* or pr_review_* would stop
--     the review workflow and main's delegated coding outright, and a
--     dedicated role cannot carve them back out because the dispatch role of a
--     workflow-triggered run is the workflow's ('service'), not the agent's.
--     For unattended agents those tools stay gated by OPT_IN_TOOLS plus the
--     runner's tools_allowed guard: only an agent whose manifest names them is
--     offered them, and a call to one it was not offered is refused.
--   * 'owner' and 'admin' keep their catch-all allow: the operator drives
--     reviews and Claude Code from chat.
--
-- Most-specific pattern wins in check_tool_permission (exact beats '*'), so
-- these exact denies beat the roles' '*' allow. A tenant that wants a tool
-- back for a role adds a tenant-scoped row, evaluated before __default__.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['user', 'member']) AS role,
     unnest(ARRAY[
         'github_create_review', 'github_reply_review_comment', 'github_resolve_threads',
         'claude_code_start', 'claude_code_followup', 'claude_code_cancel',
         'pr_review_intake', 'pr_review_prepare', 'pr_review_finalize'
     ]) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', 'service', tool, 'deny'
FROM unnest(ARRAY[
    'github_create_review', 'github_reply_review_comment', 'github_resolve_threads'
]) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__'
--      AND role IN ('service', 'user', 'member')
--      AND tool_pattern IN (
--          'github_create_review', 'github_reply_review_comment', 'github_resolve_threads',
--          'claude_code_start', 'claude_code_followup', 'claude_code_cancel',
--          'pr_review_intake', 'pr_review_prepare', 'pr_review_finalize');
