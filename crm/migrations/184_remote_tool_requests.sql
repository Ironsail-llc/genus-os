-- Migration 184: remote tools -- the request ledger and the parent_agent role.
--
-- A parent tenant's agent may run a tool a plugin here declared remote
-- (robothor/engine/remote_tools.py), through the bridge. Two things back it.
--
-- remote_tool_requests: a write runs at most once per (tenant_id, request_id).
-- The request is claimed 'running' before the tool runs and becomes 'done',
-- with the answer it gave, after. The row keeps the tool and a digest of the
-- arguments it was claimed for: a later call with the same id but another
-- tool or other arguments is refused, never answered with this row. A done request answers again from its row
-- (outcome 'replayed'); a running one is never run again -- its outcome is
-- unknown until an operator looks, because the tool may have acted.
--
-- parent_agent: the role a person-less read from the parent runs as. It is
-- denied everything by default with an explicit catch-all deny at
-- __default__; a tenant opens it one tool at a time with a tenant-scoped
-- allow row, which check_tool_permission evaluates before __default__:
--
--   INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
--   VALUES ('<this tenant>', 'parent_agent', '<tool>', 'allow');
--
-- Idempotent: every statement can run again.

CREATE TABLE IF NOT EXISTS remote_tool_requests (
    tenant_id    TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    tool         TEXT NOT NULL DEFAULT '',
    args_digest  TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done')),
    result       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, request_id)
);

-- A database that ran an earlier draft of this file gets the two columns too.
ALTER TABLE remote_tool_requests ADD COLUMN IF NOT EXISTS tool TEXT NOT NULL DEFAULT '';
ALTER TABLE remote_tool_requests ADD COLUMN IF NOT EXISTS args_digest TEXT NOT NULL DEFAULT '';

-- The platform's tenant_isolation shape (081, 164, 169).
ALTER TABLE remote_tool_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE remote_tool_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON remote_tool_requests;
CREATE POLICY tenant_isolation ON remote_tool_requests
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
VALUES ('__default__', 'parent_agent', '*', 'deny')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions WHERE tenant_id = '__default__' AND role = 'parent_agent'
--      AND tool_pattern = '*';
--   DROP TABLE IF EXISTS remote_tool_requests;
