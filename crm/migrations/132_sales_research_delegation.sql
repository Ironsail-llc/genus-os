-- Dedicated role for a bounded research parent and its native descendants.
-- Child manifests still restrict them to read tools; there are no generic
-- spawn, CRM mutation, approval, credential, or sending permissions.
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
SELECT '__default__','sales_research_agent',tool,'allow'
FROM unnest(ARRAY['web_search','web_fetch','write_file','sales_get_prospect',
 'sales_get_context','sales_research_parallel']) AS tool
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
VALUES('__default__','sales_research_agent','*','deny') ON CONFLICT DO NOTHING;
