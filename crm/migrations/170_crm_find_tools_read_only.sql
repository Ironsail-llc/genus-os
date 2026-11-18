-- Migration 170: read-only roles may use the CRM's two find-by-outside-id tools.
--
-- Viewer and member sessions are read-only by pattern: search_*, get_* and
-- list_* are allowed and '*' is denied (migrations 037, 087, 088).
-- find_company_by_identifier and find_person_by_identifier only read, but
-- match none of those patterns, so they would be refused. This allows those
-- two exact names, not a find_* pattern: a later tool that writes under a
-- find_ name must not inherit read-only access.
--
-- Idempotent; tenant overrides are untouched (these are __default__ rows).

INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'viewer', 'find_company_by_identifier', 'allow'),
    ('__default__', 'viewer', 'find_person_by_identifier', 'allow'),
    ('__default__', 'member', 'find_company_by_identifier', 'allow'),
    ('__default__', 'member', 'find_person_by_identifier', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;

-- Rollback:
--   DELETE FROM role_permissions WHERE tenant_id = '__default__'
--     AND role IN ('viewer', 'member')
--     AND tool_pattern IN ('find_company_by_identifier', 'find_person_by_identifier');
