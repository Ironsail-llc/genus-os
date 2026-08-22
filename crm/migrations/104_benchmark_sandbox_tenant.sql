-- Benchmark sandbox tenant: a real place for benchmark agents to act.
--
-- WHY. Write-heavy agents were graded on rubrics that demand action ("takes a
-- scrub/flag action", "cleans the phone field") while the harness denied every
-- write tool those rubrics grade, over records that did not exist — the
-- crm-hygiene suite named `p-9999`, which a uuid column cannot even represent.
-- The only way to score was to narrate work the agent was forbidden to do.
--
-- The fix needs somewhere isolated to write. crm_people.tenant_id and
-- crm_tasks.tenant_id are FOREIGN KEYs onto crm_tenants(id), so a benchmark
-- fixture cannot be seeded under a tenant that does not exist here first.
--
-- ISOLATION. parent_tenant_id is NULL and child_data_access is FALSE, so this
-- tenant is not reachable through the hierarchy resolution in
-- robothor/engine/permissions.py::resolve_accessible_tenants — no production
-- role inherits access to it, and the RLS policy from migration 081 refuses to
-- serve its rows to a connection bound to any other tenant.
--
-- LIFECYCLE. The tenant row is permanent; its CONTENTS are not. Every
-- benchmark task seeds its fixtures and deletes every row in this tenant when
-- it finishes (robothor/engine/benchmark_sandbox.py::teardown_sandbox), so a
-- night's leftovers can never become the next night's ambient state.
--
-- Idempotent, and re-ensured at runtime by ensure_sandbox_tenant() so a fresh
-- instance or a test database works before this migration has been applied.

INSERT INTO crm_tenants (id, display_name, parent_tenant_id, active, child_data_access)
VALUES ('benchmark-sandbox', 'Benchmark Sandbox', NULL, TRUE, FALSE)
ON CONFLICT (id) DO NOTHING;
