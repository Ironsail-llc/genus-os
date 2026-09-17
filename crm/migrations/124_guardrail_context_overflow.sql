-- Migration 124: the engine shrinking a conversation is a guardrail event.
--
-- WHY. On 2026-09-16 a production run answered twelve steps on the local
-- fallback, outgrew that model's 65,536-token window, and died on
-- `no user query found in messages` -- Ollama's reply when it has truncated a
-- conversation from the front until no user turn survives. The engine now
-- classifies that as a context overflow, shrinks the messages and re-asks the
-- same model once, which is a CONTROL: it acts, and a control that acts must
-- leave evidence, or nobody can tell it from one that is inert. Every other
-- control on this instance is counted in `agent_guardrail_events`
-- (`robothor/flags/evidence.py` keys on `guardrail_name`), and three of them
-- shipped enforcing-and-doing-nothing before anyone looked at that table.
--
-- WHAT. One more allowed value in the action CHECK. The vocabulary was
-- blocked/warned/allowed (001) plus observed (079); `context_overflow` joins
-- it rather than being folded into `warned`, because it is not a warning about
-- a policy -- it is a record that the engine rewrote a request, with the
-- before/after token estimate in `reason`.
--
-- The writer degrades to `warned` and logs once when this migration has not
-- been applied (`tracking.log_guardrail_event`), so an instance between a
-- deploy and `genus migrate` under-reports rather than silently dropping the
-- row. Applying this is what makes the count exact.
--
-- Idempotent, and in the same shape as 079: the DO block drops whatever name
-- Postgres assigned the action CHECK (including this migration's own re-add)
-- before re-adding it.

DO $$
DECLARE
    c text;
BEGIN
    IF to_regclass('public.agent_guardrail_events') IS NULL THEN
        RAISE NOTICE 'agent_guardrail_events not present; skipping 124';
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
        CHECK (action IN ('blocked', 'warned', 'allowed', 'observed', 'context_overflow'));
END $$;
