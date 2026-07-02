-- Wave-1 hardening (PR-1, migration 079): make the guardrail-event audit trail support the
-- observe → alert → enforce rollout ladder.
--
-- agent_guardrail_events has existed since migration 014 and is read by the
-- health dashboard, but nothing wrote to it until tracking.log_guardrail_event.
-- The original CHECK only allowed 'blocked'/'warned'/'allowed'. The rollout of
-- sandbox-default, RBAC-over-fleet, and fail-closed-approval needs to record
-- SHADOW decisions made in observe mode (the guardrail WOULD have acted, but
-- the flag let it through) so the operator can inspect impact before enforcing.
-- We add the 'observed' action and a `mode` column recording which enforcement
-- mode produced the event.
--
-- Idempotent: the DO block drops whatever name Postgres assigned the action
-- CHECK (including this migration's own re-add), and ADD COLUMN IF NOT EXISTS
-- is a no-op on re-run.

DO $$
DECLARE
    c text;
BEGIN
    -- Only act if the table exists (fresh installs that ran 014 will have it).
    IF to_regclass('public.agent_guardrail_events') IS NULL THEN
        RAISE NOTICE 'agent_guardrail_events not present; skipping 079';
        RETURN;
    END IF;

    SELECT conname INTO c
    FROM pg_constraint
    WHERE conrelid = 'agent_guardrail_events'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%action%';

    IF c IS NOT NULL THEN
        EXECUTE format('ALTER TABLE agent_guardrail_events DROP CONSTRAINT %I', c);
    END IF;

    ALTER TABLE agent_guardrail_events
        ADD CONSTRAINT agent_guardrail_events_action_check
        CHECK (action IN ('blocked', 'warned', 'allowed', 'observed'));

    ALTER TABLE agent_guardrail_events
        ADD COLUMN IF NOT EXISTS mode TEXT;
END $$;
