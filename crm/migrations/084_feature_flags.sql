-- crm/migrations/084_feature_flags.sql
-- The 12 governed guardrail flags become DB-backed. Env stays as the emergency
-- override (resolution: DB -> env -> coded default), so this cutover changes
-- nothing until an operator writes a row. Seeds each flag with a
-- behaviour-preserving default; the store fills any missing value from env at
-- first read.

CREATE TABLE IF NOT EXISTS feature_flags (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS feature_flag_audit (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    from_value TEXT,
    to_value   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    reason     TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feature_flag_audit_name_at
    ON feature_flag_audit (name, at DESC);

-- Seed with behaviour-preserving defaults. Ladder flags default to the coded
-- default of the reader (`observe`); booleans to their coded default. An
-- operator promotes from here; the store will not overwrite an existing row.
INSERT INTO feature_flags (name, value, updated_by, reason) VALUES
    ('ROBOTHOR_RBAC_MODE',                 'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_INJECTION_SCAN_MODE',       'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE','observe', 'migration-084', 'seed'),
    ('ROBOTHOR_APPROVAL_MODE',             'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_SANDBOX_DEFAULT_MODE',      'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_COMPLETION_CONTRACTS_MODE', 'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_7_MODE',                'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_13_MODE',               'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_1_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_RIP_4_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_RIP_5_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_JUDGE_ENABLED',             'false',   'migration-084', 'seed')
ON CONFLICT (name) DO NOTHING;
