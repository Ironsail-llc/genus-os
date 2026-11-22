-- Migration 172: workspace tasks and member approval.
--
-- * ``room_member_removals``: who was removed from a workspace, by whom. Opening
--   a task as a workspace lets the task's human assignee join it; a person who
--   was removed must not get back in that way. Only someone who can share the
--   workspace re-adds them (which clears the row).
-- * One suggestion key per workspace: an edition's suggestion becomes a task
--   once, whether it is still open, accepted or dismissed.
-- * An index for a member's own proposed actions (the Inbox's member path).
--
-- The table follows 169's conventions: tenant-keyed, forced row-level
-- security with the platform's tenant_isolation shape, a single-column
-- reference to ``rooms(id)`` so ``genus tenant move-workload`` can re-tenant
-- rows in place. Idempotent: every statement can run again.

CREATE TABLE IF NOT EXISTS room_member_removals (
    tenant_id  TEXT NOT NULL,
    room_id    UUID NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
    user_id    UUID NOT NULL,
    removed_by UUID,
    removed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, room_id, user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_room_tasks_suggestion_key
    ON room_tasks (tenant_id, room_id, suggestion_key) WHERE suggestion_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_operation_actions_proposed_for
    ON operation_actions (tenant_id, proposed_for_user_id)
    WHERE proposed_for_user_id IS NOT NULL;

ALTER TABLE room_member_removals ENABLE ROW LEVEL SECURITY;
ALTER TABLE room_member_removals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON room_member_removals;
CREATE POLICY tenant_isolation ON room_member_removals
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));
