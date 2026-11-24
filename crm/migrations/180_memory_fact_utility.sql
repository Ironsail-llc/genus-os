-- Migration 180: outcome utility on memory_facts
--
-- WHY
--   A fact's only outcome signal was outcome_failures (043): a failed run
--   penalised every fact it recalled, and nothing ever credited a fact behind
--   a run that went well. Ranking instead read raw access_count, so a fact
--   rose for being retrieved, which made it retrieved more.
--
--   These columns hold the positive evidence and the scalar computed from it:
--     outcome_successes  runs that recalled the fact and completed without failure
--     outcome_citations  of those, runs whose final answer used a distinctive
--                        value of the fact (robothor/memory/utility.py is_cited)
--     last_success_at    when the latest such run ended
--     utility            the nightly decay pass's scalar in (-1, 1), read by
--                        decay and by the recall blend
--   The counts live on the fact, not in fact_access_log, so they survive the
--   access-log roll-up (092) that trims raw rows after the retention window.
--
-- SAFETY
--   Additive: four nullable columns with no default, so ADD COLUMN is a
--   catalog-only change — no table rewrite, no scan, no index build. NULL reads
--   as zero evidence and neutral utility everywhere.
--
--   Locks: ALTER TABLE takes ACCESS EXCLUSIVE on memory_facts, held only for
--   the catalog update (milliseconds) and until this transaction commits.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS. Safe to re-run under another number.
--
-- Rollback:
--   ALTER TABLE memory_facts DROP COLUMN IF EXISTS outcome_successes,
--       DROP COLUMN IF EXISTS outcome_citations, DROP COLUMN IF EXISTS last_success_at,
--       DROP COLUMN IF EXISTS utility;

BEGIN;

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS outcome_successes INTEGER,
    ADD COLUMN IF NOT EXISTS outcome_citations INTEGER,
    ADD COLUMN IF NOT EXISTS last_success_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS utility           DOUBLE PRECISION;

COMMIT;
