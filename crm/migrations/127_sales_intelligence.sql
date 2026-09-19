-- Native sales workers have narrow RBAC in addition to manifest allow-lists.
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
SELECT '__default__','sales_agent',tool,'allow'
FROM unnest(ARRAY['web_search','web_fetch','write_file','sales_get_prospect',
 'sales_get_context','sales_discover','sales_propose_email']) AS tool
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
VALUES('__default__','sales_agent','*','deny') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS sales_settings (
 tenant_id TEXT PRIMARY KEY, config JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sales_prospects (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, company_id UUID NOT NULL REFERENCES crm_companies(id),
 domain TEXT NOT NULL, name TEXT NOT NULL, source_url TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'discovered', version INTEGER NOT NULL DEFAULT 0,
 dossier JSONB, qualification JSONB, review_note TEXT, owner TEXT NOT NULL DEFAULT 'agent',
 external_company_id TEXT, pipedrive_ids JSONB NOT NULL DEFAULT '{}',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,domain), UNIQUE(tenant_id,external_company_id)
);
CREATE TABLE IF NOT EXISTS sales_contacts (
 id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, prospect_id UUID NOT NULL REFERENCES sales_prospects(id),
 person_id UUID NOT NULL REFERENCES crm_people(id), email TEXT NOT NULL, data JSONB NOT NULL,
 UNIQUE(tenant_id,prospect_id,email)
);
CREATE TABLE IF NOT EXISTS sales_policies (
 tenant_id TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL, data JSONB NOT NULL,
 approved_by TEXT NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,kind,version)
);
CREATE TABLE IF NOT EXISTS sales_suppression (
 tenant_id TEXT NOT NULL, email TEXT NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,email)
);
CREATE TABLE IF NOT EXISTS sales_outcomes (
 tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, prospect_id UUID NOT NULL REFERENCES sales_prospects(id),
 kind TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, order_ref TEXT,
 PRIMARY KEY(tenant_id,event_id)
);
CREATE TABLE IF NOT EXISTS sales_messages (
 tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL, prospect_id UUID NOT NULL REFERENCES sales_prospects(id),
 direction TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL,
 PRIMARY KEY(tenant_id,provider_id)
);
ALTER TABLE sales_prospects ADD COLUMN IF NOT EXISTS conversation_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS sales_dossier_history (
 tenant_id TEXT NOT NULL, prospect_id UUID NOT NULL REFERENCES sales_prospects(id),
 version INTEGER NOT NULL, dossier JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,prospect_id,version)
);
CREATE INDEX IF NOT EXISTS sales_messages_prospect ON sales_messages(tenant_id,prospect_id,occurred_at);
CREATE INDEX IF NOT EXISTS sales_outcomes_prospect ON sales_outcomes(tenant_id,prospect_id,occurred_at);
CREATE TABLE IF NOT EXISTS sales_send_slots (
 tenant_id TEXT NOT NULL, action_id UUID NOT NULL REFERENCES operation_actions(id),
 sender TEXT NOT NULL, send_date DATE NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,action_id)
);
CREATE INDEX IF NOT EXISTS sales_send_slots_daily ON sales_send_slots(tenant_id,sender,send_date);
ALTER TABLE sales_send_slots ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_send_slots FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_send_slots;
CREATE POLICY tenant_isolation ON sales_send_slots
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));
ALTER TABLE sales_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_settings;
CREATE POLICY tenant_isolation ON sales_settings
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_prospects ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_prospects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_prospects;
CREATE POLICY tenant_isolation ON sales_prospects
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_contacts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_contacts;
CREATE POLICY tenant_isolation ON sales_contacts
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_policies FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_policies;
CREATE POLICY tenant_isolation ON sales_policies
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_suppression ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_suppression FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_suppression;
CREATE POLICY tenant_isolation ON sales_suppression
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_outcomes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_outcomes;
CREATE POLICY tenant_isolation ON sales_outcomes
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_messages;
CREATE POLICY tenant_isolation ON sales_messages
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));

ALTER TABLE sales_dossier_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_dossier_history FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sales_dossier_history;
CREATE POLICY tenant_isolation ON sales_dossier_history
 USING (tenant_id = current_setting('app.tenant_id',true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id',true));
