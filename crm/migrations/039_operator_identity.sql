-- ─────────────────────────────────────────────────────────────────────────────
-- 039_operator_identity.sql
--
-- Links the identity layer (tenant_users) to the CRM rolodex (crm_people)
-- so the platform has a single authoritative answer to "who is the operator?".
--
-- Before this migration, operator identity lived only in the env vars
-- ROBOTHOR_OWNER_EMAIL / ROBOTHOR_OWNER_NAME. Name-based contact resolution
-- (search_people ILIKE) could not distinguish the operator from other CRM
-- contacts sharing a first name — risking confidential info reaching the
-- wrong "Philip" / "Alice" / etc.
--
-- After this migration, tenant_users.person_id is the canonical pointer,
-- DB-enforced unique per (tenant_id, role='owner'). Bootstrap is handled
-- by robothor.crm.dal.bootstrap_owner_person_links() on daemon startup,
-- driven by ~/.robothor/owner.yaml.
--
-- Rollback:
--   DROP INDEX IF EXISTS uq_tenant_users_owner_person;
--   DROP INDEX IF EXISTS idx_tenant_users_person;
--   ALTER TABLE tenant_users DROP COLUMN IF EXISTS person_id;
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE tenant_users
    ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES crm_people(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_users_person
    ON tenant_users(person_id) WHERE person_id IS NOT NULL;

-- Only one owner-linked person per tenant. Nullable person_id still allowed
-- multiple times (supports the pre-bootstrap state).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_users_owner_person
    ON tenant_users(tenant_id)
    WHERE role = 'owner' AND person_id IS NOT NULL;

COMMIT;
