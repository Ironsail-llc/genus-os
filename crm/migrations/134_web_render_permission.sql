-- Read-only rendered public pages are a separate capability from an interactive
-- browser. Only the bounded research role gains this permission automatically.
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
VALUES('__default__','sales_research_agent','web_render','allow')
ON CONFLICT DO NOTHING;
