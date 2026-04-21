-- Migration 037: Add scheduling_policy to crm_people
--
-- Tracks how aggressively agents may schedule calendar invites for a person.
-- Values:
--   'stable'    (default)  — agents may auto-schedule within normal guardrails.
--   'ask_first'             — agents must send a proposal email and receive
--                             explicit confirmation before creating an invite.
--                             Used for people whose availability is not fully
--                             reflected in their calendar (variable timezones,
--                             selective calendar sharing).
--   'no_auto'               — agents may not create invites for this person at
--                             all — only the operator can. Use for VIPs or
--                             anyone who has explicitly asked agents to
--                             stand down from auto-scheduling.
--
-- Per-person policies are set by the operator post-migration, e.g.:
--   UPDATE crm_people SET scheduling_policy = 'ask_first' WHERE email = '…';
-- The recurring_meeting_proposal_required guardrail reads this column to
-- decide whether a proposal email is required before creating an invite.

BEGIN;

ALTER TABLE crm_people
    ADD COLUMN IF NOT EXISTS scheduling_policy TEXT NOT NULL DEFAULT 'stable';

ALTER TABLE crm_people
    DROP CONSTRAINT IF EXISTS crm_people_scheduling_policy_check;

ALTER TABLE crm_people
    ADD CONSTRAINT crm_people_scheduling_policy_check
    CHECK (scheduling_policy IN ('stable', 'ask_first', 'no_auto'));

COMMIT;
