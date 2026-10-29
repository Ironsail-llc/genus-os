-- Migration 154: the audit log records which tenant an event happened in.
--
-- audit_log had no tenant: the export was appliance-wide, so no child tenant's
-- admin could be given its own trail without being handed every tenant's. The
-- audit logger now writes the enclosing tenant scope (a bridge request's
-- ACTING tenant), else the process's own tenant, and every read names the
-- tenants it may see.
--
--   * NULL is allowed and is what every row written before this migration
--     keeps: "appliance-wide". Only a root operator at home reads those.
--   * No RLS on this table. Reads filter on tenant_id explicitly; the table is
--     also read by appliance-wide jobs (retention, SIEM) that must see it all.
--   * A plain CREATE INDEX: the migration runner executes each file inside one
--     transaction, where CONCURRENTLY is not allowed.

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS tenant_id TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_timestamp
    ON audit_log (tenant_id, timestamp DESC);
