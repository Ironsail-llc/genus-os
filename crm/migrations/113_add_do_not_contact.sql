-- 113: Add do_not_contact flag to crm_people
-- Rationale: CAN-SPAM / outreach opt-out requests need a persistent flag
-- that all outbound email pipelines check before sending.

BEGIN;

ALTER TABLE crm_people
    ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_crm_people_dnc
    ON crm_people (do_not_contact)
    WHERE do_not_contact = TRUE;

COMMENT ON COLUMN crm_people.do_not_contact IS
    'TRUE = person opted out of all outreach. Email pipelines MUST check this before sending.';

COMMIT;
