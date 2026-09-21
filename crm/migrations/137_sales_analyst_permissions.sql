-- Analytics can read measured reports and write its own local telemetry only.
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
SELECT '__default__',role,tool,'allow'
FROM unnest(ARRAY['sales_analyst','sales_coordinator']) AS role,
     unnest(ARRAY['sales_get_report','write_file']) AS tool
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(tenant_id,role,tool_pattern,access)
VALUES('__default__','sales_analyst','*','deny') ON CONFLICT DO NOTHING;
