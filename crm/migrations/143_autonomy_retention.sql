-- Owner erasure and a bounded life for the private observation archive.
--
-- `autonomy_terms_snapshots` holds the rendered review page: the owner's name,
-- date of birth, address and the answers they typed into a website. It had no
-- owner-facing delete and no expiry, so it kept all of that forever, sealed
-- with a key derived from the vault master key. Neither did
-- `autonomy_payment_events`, which holds money facts.
--
-- `redacted_at` records an erasure that KEEPS the audit fact — a snapshot of
-- this phase was taken at this time, under this grant version, covering this
-- many documents — and drops only the content. An audit trail that can be
-- made to forget that it ever observed anything is not an audit trail; one
-- that can never forget what it observed is a liability.
ALTER TABLE autonomy_terms_snapshots ADD COLUMN IF NOT EXISTS redacted_at timestamptz;

-- Both sweeps order by age, and both tables are otherwise only ever read by
-- (tenant, owner, operation).
CREATE INDEX IF NOT EXISTS autonomy_terms_expiry
    ON autonomy_terms_snapshots(created_at);
CREATE INDEX IF NOT EXISTS autonomy_payment_expiry
    ON autonomy_payment_events(created_at);
