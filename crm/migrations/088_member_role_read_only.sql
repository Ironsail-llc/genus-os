-- Migration 088: Tighten __default__ member role to read-only (Task 5 review, Finding 5)
--
-- WHY. Migration 087 tried to seed a member deny-all guardrail row
--   ('__default__', 'member', '*', 'deny')
-- but it collided with migration 071's earlier
--   ('__default__', 'member', '*', 'allow')
-- on the UNIQUE (tenant_id, role, tool_pattern) constraint, so
-- ON CONFLICT DO NOTHING silently skipped it. 087 documented this
-- explicitly as an intentional no-op and deferred the real tightening to a
-- deliberate follow-up rather than smuggle a live behavior change into a
-- migration that read as "additive". This migration IS that follow-up: the
-- operator has approved tightening 'member' to the same read-only shape as
-- 'viewer' (search_*/get_*/list_* allow, everything else deny).
--
-- HOW. This mutates the row 071 created, in place, via UPDATE — not a
-- blind INSERT that would collide again. There is direct precedent for
-- mutating a row/object an earlier migration created: migration 083
-- replaced the RLS policy migration 081 had created (DROP POLICY + CREATE
-- POLICY) because the original definition turned out to be wrong in
-- production. Root CLAUDE.md's "never edit an applied migration" means
-- don't go back and change 071's or 087's *file* — it says nothing about a
-- later, independently-numbered migration correcting data an earlier one
-- seeded, which is the normal shape of a migration-based rollback/fix.
--
-- SCOPE. Surgical: only the row keyed exactly
--   (tenant_id='__default__', role='member', tool_pattern='*', access='allow')
-- is touched. No other tenant_id, no other role (user/admin/owner
-- explicitly untouched per the binding constraint), and no tool_pattern
-- other than the bare '*' catch-all.
--
-- IDEMPOTENT. Second application matches zero rows (access is already
-- 'deny') and is a no-op. An environment where 071 never ran has no row to
-- match either, and is also a no-op — see the defensive INSERT below for
-- why that's still safe.

UPDATE role_permissions
SET access = 'deny'
WHERE tenant_id = '__default__'
  AND role = 'member'
  AND tool_pattern = '*'
  AND access = 'allow';

-- Defensive: guarantee the read-only allow rows exist even in an
-- environment where 087 never ran (087 should always precede 088 in
-- migration order, but this keeps 088 self-sufficient rather than
-- depending on file ordering for correctness). Mirrors 'viewer's seeded
-- shape from migration 037. Idempotent no-op wherever 087 already applied.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'member', 'search_*', 'allow'),
    ('__default__', 'member', 'get_*', 'allow'),
    ('__default__', 'member', 'list_*', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   UPDATE role_permissions SET access = 'allow'
--   WHERE tenant_id = '__default__' AND role = 'member'
--     AND tool_pattern = '*' AND access = 'deny';
