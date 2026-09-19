-- A bounded operator/agent research brief and its newly discovered companies.
CREATE TABLE IF NOT EXISTS sales_requests (
 tenant_id TEXT NOT NULL, id UUID NOT NULL, request_key TEXT NOT NULL,
 config JSONB NOT NULL, policy_version TEXT NOT NULL, created_by TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
 revision INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,request_key)
);
CREATE TABLE IF NOT EXISTS sales_request_members (
 tenant_id TEXT NOT NULL, request_id UUID NOT NULL, prospect_id UUID NOT NULL REFERENCES sales_prospects(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,prospect_id),
 FOREIGN KEY(tenant_id,request_id) REFERENCES sales_requests(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS sales_request_members_request ON sales_request_members(tenant_id,request_id);
ALTER TABLE sales_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_requests;
CREATE POLICY tenant_isolation ON sales_requests
 USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
ALTER TABLE sales_request_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_request_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_request_members;
CREATE POLICY tenant_isolation ON sales_request_members
 USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true));
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
SELECT '__default__','sales_coordinator',tool,'allow'
FROM unnest(ARRAY['sales_get_workspace','sales_create_request','sales_get_request','sales_get_context','sales_get_prospect','write_file']) AS tool
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
VALUES('__default__','sales_coordinator','*','deny') ON CONFLICT DO NOTHING;
