-- Migration 095: bi-temporal validity + a reviewable record of conflict decisions
--
-- WHY
--   conflicts.py collapses "contradiction" and "update" into one supersede:
--
--       if classification in ("contradiction", "update"):
--           store new; deactivate old
--
--   Those are different events. An UPDATE means the world changed — "the
--   meeting moved to 4pm" — and the old fact was TRUE until it wasn't. A
--   CONTRADICTION means one of the two claims is simply WRONG. With a single
--   time axis there is no way to express the difference, so both become
--   "old row disappears" and the system forgets that the 3pm meeting was ever
--   real. That is precisely the long-horizon failure this overhaul targets:
--   an agent asked "what did we decide last month" cannot answer, because
--   last month's truth was deleted rather than bounded.
--
--   valid_from / valid_to give facts a second time axis (when the fact was TRUE
--   in the world) distinct from created_at / updated_at (when we learned it).
--   Zep is the reference design here and the only published conflict-resolution
--   algorithm in the field.
--
-- WHY THE DECISIONS TABLE
--   The LLM classification that drives supersession is currently persisted
--   NOWHERE. It deactivates rows and vanishes. Its error rate has therefore
--   never been measured and cannot be, which is how a classifier biased toward
--   "new" went unnoticed. Append-only decision rows make the judgement
--   reviewable after the fact and give a wrong supersession a paper trail to be
--   reverted from.
--
-- SAFETY
--   Purely additive. Both columns are NULL for all 152k existing rows and no
--   read path filters on them yet, so retrieval is byte-identical until
--   MEMORY_BITEMPORAL is flipped. Nothing is deactivated or retired here.
--
-- IDEMPOTENT: ADD COLUMN / CREATE TABLE / CREATE INDEX are IF NOT EXISTS.
-- Rollback:
--   ALTER TABLE memory_facts DROP COLUMN IF EXISTS valid_from, DROP COLUMN IF EXISTS valid_to;
--   DROP TABLE IF EXISTS memory_conflict_decisions;

BEGIN;

-- valid_from NULL means "no known start" (every pre-existing fact), NOT "never
-- valid" — the point-in-time filter has to treat NULL as unbounded or the
-- entire backlog becomes invisible the moment the flag flips.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to   TIMESTAMPTZ;

-- Partial: only bounded facts are interesting to a point-in-time query, and
-- that is a small minority of the table.
CREATE INDEX IF NOT EXISTS idx_facts_valid_to
    ON memory_facts(valid_to)
    WHERE valid_to IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_conflict_decisions (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    -- The fact being written, and the existing fact it was compared against.
    new_fact_id     BIGINT,
    existing_fact_id BIGINT,
    -- new | duplicate | update | contradiction | reinforced
    classification  TEXT NOT NULL,
    reasoning       TEXT NOT NULL DEFAULT '',
    similarity      DOUBLE PRECISION,
    -- What we actually DID, which can differ from the classification once
    -- policy sits between them.
    action          TEXT NOT NULL DEFAULT '',
    new_fact_text   TEXT NOT NULL DEFAULT '',
    existing_fact_text TEXT NOT NULL DEFAULT '',
    -- Set by a human or a later audit that judged this decision wrong. NULL
    -- means unreviewed, which is not the same as correct.
    review_verdict  TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conflict_decisions_decided
    ON memory_conflict_decisions(decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_conflict_decisions_class
    ON memory_conflict_decisions(classification, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_conflict_decisions_existing
    ON memory_conflict_decisions(existing_fact_id)
    WHERE existing_fact_id IS NOT NULL;

-- Migration 081 FORCEs RLS on the tables that existed then; a new tenant-scoped
-- table needs its own policy or it is readable across tenants.
ALTER TABLE memory_conflict_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_conflict_decisions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON memory_conflict_decisions;
CREATE POLICY tenant_isolation ON memory_conflict_decisions
    USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    )
    WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    );

COMMIT;
