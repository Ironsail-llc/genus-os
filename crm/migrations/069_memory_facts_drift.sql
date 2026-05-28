-- Rip 7: External-drift detection for memory_facts.
--
-- Adapted from the Hermes Agent `_detect_external_drift` pattern
-- (`tools/memory_tool.py:516-569`), which snapshots a file to
-- `.bak.<ts>` and refuses the next mutation when on-disk content
-- wouldn't round-trip the tool's parser. SQL adaptation: a
-- per-row SHA-256 over the user-visible columns plus a tenant-
-- scoped audit table that captures the state of a row when an
-- out-of-band UPDATE is detected.
--
-- Enforcement is application-side; this migration only provides
-- the storage. See `robothor.memory.facts.update_fact()` for the
-- writer and `robothor.engine.feature_flags.is_rip_enabled(7)`
-- for the kill switch. The detector runs in observe-only mode
-- for 7 days, alert-only for 7 days, then enforce, per the
-- upgrade plan.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── content_hash column ──────────────────────────────────────
-- SHA-256 over (fact_text || tenant_id || category || person_id).
-- Updated on every legitimate write; mismatch on UPDATE means
-- another writer touched the row out of band.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_facts_content_hash
    ON memory_facts(content_hash);

-- Backfill existing rows with their current canonical hash so
-- the writer can distinguish "first-touch since migration" from
-- "real drift" on the very next UPDATE.
UPDATE memory_facts
SET content_hash = encode(
        digest(
            COALESCE(fact_text, '')
            || '|' || COALESCE(tenant_id, '')
            || '|' || COALESCE(category, '')
            || '|' || COALESCE(person_id::text, ''),
            'sha256'
        ),
        'hex'
    )
WHERE content_hash IS NULL;

-- ── audit table ──────────────────────────────────────────────
-- Snapshot of the row state at the moment drift is detected.
-- Append-only; never updated; retained indefinitely. The writer
-- inserts a row here before refusing (or before applying the
-- newer in-memory value in observe-only mode).
CREATE TABLE IF NOT EXISTS memory_facts_audit (
    snapshot_id BIGSERIAL PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    snapshot_at TIMESTAMPTZ DEFAULT NOW(),
    fact_text TEXT,
    content_hash_at_snapshot TEXT,
    content_hash_expected TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_audit_fact
    ON memory_facts_audit(fact_id, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS idx_facts_audit_tenant
    ON memory_facts_audit(tenant_id, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS idx_facts_audit_reason
    ON memory_facts_audit(reason, snapshot_at DESC);
