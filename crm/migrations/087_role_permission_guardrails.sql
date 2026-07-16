-- Migration 087: role_permissions guardrail rows (Task 5, Unified Identity Context)
--
-- Two additive, self-documenting seeds. Neither changes any existing role's
-- effective behavior — see the per-block notes.
--
-- ── guest: explicit deny-all ────────────────────────────────────────
-- check_tool_permission() already fails closed for a role with zero
-- matching rules ("No permission rules for role ... — access denied"), so an
-- unconfigured 'guest' role is already denied everything. This row makes
-- that denial explicit and self-documenting in the data instead of relying
-- on an operator reading the Python fail-closed comment.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'guest', '*', 'deny')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- ── member: read-only-by-default intent ─────────────────────────────
-- IMPORTANT: migration 071 (user_accounts) already seeded
--   ('__default__', 'member', '*', 'allow')
-- as a deliberate, already-shipped decision ("mirror 'user' so the existing
-- permissions.py works unchanged"). The UNIQUE (tenant_id, role, tool_pattern)
-- constraint means the '*' row below is a no-op here (ON CONFLICT DO
-- NOTHING skips it) — member's full-access default is intentionally left
-- untouched by this migration, matching the binding constraint "do NOT touch
-- existing user/admin/owner rows" (member inherited that same guarantee via
-- 071). The search_*/get_*/list_* rows are added for documentation/future
-- parity with 'viewer' and are harmless no-ops today: they're strictly
-- narrower than the '*' allow rule that already wins ties for those
-- patterns via the specificity metric, so they change nothing for any
-- tenant that already ran 071 (i.e. every tenant this migration will ever
-- run against).
--
-- Tightening 'member' to an actual read-only default is a live behavior
-- change for already-provisioned accounts and belongs behind its own
-- rollout flag + explicit operator decision, not a silent migration edit of
-- an applied row (root CLAUDE.md: "never edit an applied migration"; a live
-- UPDATE/DELETE against a row 071 already shipped is the same hazard by
-- another name). If/when the operator wants member tightened for real,
-- that's a follow-up migration that either deletes the 071 row explicitly
-- or ships behind an enforcement-mode flag like the rest of this task.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'member', 'search_*', 'allow'),
    ('__default__', 'member', 'get_*', 'allow'),
    ('__default__', 'member', 'list_*', 'allow'),
    ('__default__', 'member', '*', 'deny')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions WHERE tenant_id='__default__' AND role='guest';
--   DELETE FROM role_permissions WHERE tenant_id='__default__' AND role='member'
--     AND tool_pattern IN ('search_*', 'get_*', 'list_*');
--   -- (the member '*' deny row was never inserted — 071's '*' allow row wins the conflict)
