-- Recover only owner-requested read-only checks, with bounded fenced leases.
ALTER TABLE autonomy_handoffs ADD COLUMN IF NOT EXISTS check_token uuid;
ALTER TABLE autonomy_handoffs ADD COLUMN IF NOT EXISTS check_lease_until timestamptz;
ALTER TABLE autonomy_handoffs ADD COLUMN IF NOT EXISTS check_attempts integer NOT NULL DEFAULT 0 CHECK (check_attempts >= 0);
CREATE INDEX IF NOT EXISTS autonomy_handoff_checks ON autonomy_handoffs(updated_at)
    WHERE state='checking';
