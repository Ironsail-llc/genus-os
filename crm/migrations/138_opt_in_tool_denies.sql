-- Migration 138: make the sales tools and web_render opt-in permissions.
--
-- Migration 134 added ('__default__','sales_research_agent','web_render','allow')
-- under the comment "Only the bounded research role gains this permission
-- automatically". That was not true when it shipped. Migration 037 seeds
-- ('__default__','user','*','allow') and migration 107 seeds
-- ('__default__','service','*','allow') -- and 'service' is the role every
-- automated agent run carries by default -- so web_render was already allowed
-- for every default-role caller, and 134 was an additive grant to a role that
-- separately holds its own '*' deny. The same applied to all ten sales_* tools:
-- neither sales_discover nor sales_propose_email sends anything, but both are
-- unbounded CRM writes that any default-role agent could make.
--
-- This migration adds the denies that make 134's comment true.
--
-- How check_tool_permission resolves these (robothor/engine/permissions.py):
-- tenant rules first, then __default__; within a level the most specific
-- pattern wins (exact match, then count of non-glob characters), and a deny
-- breaks a true tie. So:
--
--   * 'web_render' (exact) beats '*' -> denied for the roles below.
--   * 'sales_*' (6 literal characters) beats '*' -> denied for the roles below.
--   * An exact per-role allow -- 134's web_render for sales_research_agent,
--     127's sales_discover for sales_agent -- is a different role's row and is
--     untouched.
--   * A tenant that wants either capability back adds a tenant-scoped row,
--     which is evaluated before __default__ entirely.
--
-- Scope: 'service', 'user' and 'member' only. 'admin' and 'owner' keep their
-- catch-all allow: those are the operator's own roles, not an agent's, and the
-- operator has to be able to drive their own sales deployment.
--
-- Advertisement is gated separately, in OPT_IN_TOOLS
-- (robothor/engine/tools/constants.py): an agent with no `tools_allowed` is no
-- longer offered these schemas at all. Both halves are needed -- a tool an
-- agent cannot see is still a tool it can name.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
SELECT '__default__', role, tool, 'deny'
FROM unnest(ARRAY['service', 'user', 'member']) AS role,
     unnest(ARRAY['web_render', 'sales_*']) AS tool
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- One exception, and it is not a hole. `sales_process_queue` is the native
-- workflow's own pump: it is invoked by the engine's workflow runner, whose
-- identity IS role 'service', so the glob deny above would stop the sales
-- pipeline from running at all. Its gate is not RBAC -- the handler refuses
-- any caller whose agent_id is not "workflow:<id>", whose user_id is not
-- "service:<agent_id>" and whose role is not 'service'
-- (robothor/engine/tools/handlers/sales.py). An exact pattern beats the
-- 'sales_*' glob, so this restores exactly that one tool and nothing else.
-- test_sales_queue_identity_gate.py pins the handler gate that is now the
-- only thing standing behind this row.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
VALUES ('__default__', 'service', 'sales_process_queue', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions
--    WHERE tenant_id = '__default__'
--      AND role IN ('service', 'user', 'member')
--      AND tool_pattern IN ('web_render', 'sales_*', 'sales_process_queue');
