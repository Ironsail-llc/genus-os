-- Migration 151: the review worker and the review report, under migration
-- 146's pattern.
--
--   * pr_review_work runs the queued reviews itself: it starts Claude Code
--     review jobs and posts their results on GitHub and in Chat. The
--     pr-review-run workflow calls it as 'service', which keeps its
--     catch-all allow.
--   * pr_review_status is read-only and calls no model, but it names the
--     instance's pull requests, their authors and their review state: the
--     operator's view. Main's heartbeat ('service') and the operator
--     ('owner'/'admin') keep it.
--
-- 'user' and 'member' -- humans other than the operator -- are denied both,
-- as 146 denies them the other pr_review_* tools. An exact pattern beats the
-- roles' '*' allow; a tenant that wants either back for a role adds a
-- tenant-scoped row.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['user', 'member']) AS role,
     unnest(ARRAY['pr_review_work', 'pr_review_status']) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__' AND role IN ('user', 'member')
--      AND tool_pattern IN ('pr_review_work', 'pr_review_status');
