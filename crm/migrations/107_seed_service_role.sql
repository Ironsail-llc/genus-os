-- Seed the `service` role, which no migration has ever created.
--
-- WHY. Every system-triggered run — cron, hook, workflow, sub-agent — is
-- gated by `classify_system_tool_access(agent_config.service_role, ...)`, and
-- the fleet-wide default for `service_role` is the literal string "service".
-- `check_tool_permission` fails CLOSED when a role has no rules at all:
--
--     No permission rules for role 'service' — access denied
--
-- Migration 037 seeds viewer/user/admin/owner/member/guest. It does not seed
-- `service`. The production instance has the rule — inserted BY HAND on
-- 2026-07-02, the day RBAC went to enforce, which is exactly what
-- `infra/flags.yaml` records as "46 blocks day one (allow-all service
-- default)". Those 46 blocks were this, and the repair went into one
-- database instead of into the platform.
--
-- So on any fresh install that turns RBAC on — and the shipped systemd
-- drop-in sets ROBOTHOR_RBAC_ENABLED=1 with ROBOTHOR_RBAC_MODE=enforce —
-- every scheduled agent is denied every tool. Found 2026-08-24 by standing up
-- a clean containerised instance for the WildClawBench harness: the agent
-- called `exec` three times and `read_file` once, and all four were refused.
--
-- WHY ALLOW-ALL. Because that is the behaviour this role has actually had in
-- production for two months, and a migration is the wrong place to change a
-- security posture silently. `service` is the *unattended* identity: it has
-- no interactive user to approve anything, and the controls that genuinely
-- constrain it are the tool allowlist in each agent's manifest, the guardrail
-- engine, and the sandbox. Tightening this role is a deliberate operator act
-- — `robothor auth` and the role_permissions table are how — not a side
-- effect of upgrading.
--
-- Idempotent: an instance that already inserted the row by hand keeps it.

INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
VALUES ('__default__', 'service', '*', 'allow')
ON CONFLICT DO NOTHING;
