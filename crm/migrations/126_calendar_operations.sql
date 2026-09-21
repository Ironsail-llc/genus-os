BEGIN;
CREATE TABLE IF NOT EXISTS calendar_operations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    user_id text NOT NULL DEFAULT '',
    agent_id text NOT NULL,
    calendar_id text NOT NULL,
    event_id text NOT NULL,
    arguments jsonb NOT NULL,
    draft_event jsonb,
    status text NOT NULL CHECK (status IN ('draft', 'executing', 'completed', 'blocked')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calendar_operations_resource
    ON calendar_operations (tenant_id, calendar_id, event_id, updated_at DESC);
ALTER TABLE calendar_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON calendar_operations;
CREATE POLICY tenant_isolation ON calendar_operations
    USING (current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true));
COMMIT;
