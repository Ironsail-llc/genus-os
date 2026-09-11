-- 115: Backfill error_type='sandbox_denied' onto historical benchmark-sandbox
-- refusals logged before that classification existed.
--
-- check_tool_degradation and check_tool_outage (robothor/engine/detectors.py)
-- now exclude error_type='sandbox_denied' rows so a benchmark run correctly
-- refused a write is never read as a broken tool -- but that only protects
-- rows written after the classification landed. Measured 2026-09-11 15:08,
-- agent_tool_events already held rows from all three refusal families:
-- crm.py/memory.py's "benchmark sandbox: <tool> writes are disabled", and
-- gws.py's "Tool '<tool>' is disabled in benchmark mode." -- both logged with
-- error_type=NULL or a generic classification, invisible to the new filter
-- until relabeled here.
--
-- Numbered 115, not 114: a parallel branch has already claimed 114.
--
-- Idempotent: a row already labelled 'sandbox_denied' no longer matches
-- either LIKE pattern (the label isn't the message), and a row that never
-- matched stays untouched -- re-running finds nothing left to update.

BEGIN;

UPDATE agent_tool_events
SET error_type = 'sandbox_denied'
WHERE NOT success
  AND (
    error_message LIKE 'benchmark sandbox:%'
    OR error_message LIKE '%disabled in benchmark mode%'
  );

COMMIT;
