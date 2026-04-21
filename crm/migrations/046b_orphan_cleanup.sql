-- ─────────────────────────────────────────────────────────────────────────────
-- 046b_orphan_cleanup.sql
--
-- Data cleanup that pairs with 046_person_linkage.sql.
--
-- Caught during the first 046 apply attempt: contact_identifiers had rows
-- whose person_id pointed to crm_people IDs that no longer existed (the
-- pre-FK days). When 046 added person_id FKs to other tables and tried
-- to backfill from contact_identifiers, the FK constraint refused the
-- ghost UUIDs.
--
-- Fix: NULL the orphan pointers so the table invariant becomes
--     "if person_id is set, the person exists."
-- Idempotent — safe to re-run; converges to the same end state.
--
-- Rollback: there is no rollback. The orphans were already broken; we are
-- making the breakage explicit (NULL) instead of implicit (dangling UUID).
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

UPDATE contact_identifiers ci
   SET person_id = NULL
 WHERE ci.person_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM crm_people p WHERE p.id = ci.person_id
   );

COMMIT;
