-- Migration 169: company hierarchy, company identifiers and affiliations.
--
-- The CRM held people and companies as flat lists, and a plugin syncing an
-- organization chart from an outside system had only names to match on. This
-- adds three things, all tenant-scoped:
--
--   crm_companies.parent_company_id  which company sits under which. Same
--       tenant only, never the company itself or one of its descendants. Both
--       rules are enforced here, under a per-tenant advisory lock, so two
--       concurrent re-parents cannot close a loop between them.
--   crm_company_identifiers          a company's id in an outside system,
--       (tenant, system, identifier) -> company. People already have this in
--       contact_identifiers (channel = system name).
--   crm_affiliations                 a person's role at a company, unique on
--       (tenant, person, company, role). Closing one sets is_active = false
--       and until; nothing is deleted.
--
-- Additive and forward-only: one nullable column with no default and two new
-- tables, so existing rows are untouched. Safe to re-run.

-- ── Hierarchy ───────────────────────────────────────────────────────────────

ALTER TABLE crm_companies
    ADD COLUMN IF NOT EXISTS parent_company_id UUID
    REFERENCES crm_companies(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'crm_companies_parent_not_self'
          AND conrelid = 'crm_companies'::regclass
    ) THEN
        ALTER TABLE crm_companies ADD CONSTRAINT crm_companies_parent_not_self
            CHECK (parent_company_id IS NULL OR parent_company_id <> id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_crm_companies_parent
    ON crm_companies (tenant_id, parent_company_id)
    WHERE parent_company_id IS NOT NULL;

-- Runs as the caller, so under row-level security a parent in another tenant
-- is simply not found: refused the same way as a missing one.
CREATE OR REPLACE FUNCTION crm_companies_parent_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_tenant TEXT;
    cursor_id UUID;
    depth INT := 0;
BEGIN
    IF NEW.parent_company_id IS NULL THEN
        RETURN NEW;
    END IF;
    -- Serialize hierarchy writes per tenant. Each statement below then reads
    -- what a concurrent writer committed before releasing the lock.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('crm_company_hierarchy:' || COALESCE(NEW.tenant_id, ''), 0)
    );
    SELECT tenant_id INTO parent_tenant FROM crm_companies WHERE id = NEW.parent_company_id;
    IF NOT FOUND OR parent_tenant IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION 'parent company % is not a company of this tenant', NEW.parent_company_id
            USING ERRCODE = 'check_violation';
    END IF;
    cursor_id := NEW.parent_company_id;
    WHILE cursor_id IS NOT NULL LOOP
        IF cursor_id = NEW.id THEN
            RAISE EXCEPTION 'company % cannot be placed under its own descendant', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        depth := depth + 1;
        IF depth > 1000 THEN
            RAISE EXCEPTION 'company hierarchy deeper than 1000 levels'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT parent_company_id INTO cursor_id FROM crm_companies WHERE id = cursor_id;
    END LOOP;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS crm_companies_parent_guard ON crm_companies;
CREATE TRIGGER crm_companies_parent_guard
    BEFORE INSERT OR UPDATE OF parent_company_id, tenant_id ON crm_companies
    FOR EACH ROW EXECUTE FUNCTION crm_companies_parent_guard();

-- ── Shared guard: a link row belongs to the tenant of what it links ─────────

CREATE OR REPLACE FUNCTION crm_link_tenant_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM crm_companies WHERE id = NEW.company_id AND tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'company % is not a company of tenant %', NEW.company_id, NEW.tenant_id
            USING ERRCODE = 'check_violation';
    END IF;
    -- Nested, not AND-ed: PL/pgSQL does not short-circuit, and NEW has no
    -- person_id on crm_company_identifiers.
    IF TG_TABLE_NAME = 'crm_affiliations' THEN
        IF NOT EXISTS (
            SELECT 1 FROM crm_people WHERE id = NEW.person_id AND tenant_id = NEW.tenant_id
        ) THEN
            RAISE EXCEPTION 'person % is not a person of tenant %', NEW.person_id, NEW.tenant_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

-- ── Company identifiers ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_company_identifiers (
    tenant_id TEXT NOT NULL REFERENCES crm_tenants(id),
    system TEXT NOT NULL CHECK (btrim(system) <> ''),
    identifier TEXT NOT NULL CHECK (btrim(identifier) <> ''),
    company_id UUID NOT NULL REFERENCES crm_companies(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, system, identifier)
);

CREATE INDEX IF NOT EXISTS idx_crm_company_identifiers_company
    ON crm_company_identifiers (company_id);

DROP TRIGGER IF EXISTS crm_company_identifiers_tenant_guard ON crm_company_identifiers;
CREATE TRIGGER crm_company_identifiers_tenant_guard
    BEFORE INSERT OR UPDATE ON crm_company_identifiers
    FOR EACH ROW EXECUTE FUNCTION crm_link_tenant_guard();

ALTER TABLE crm_company_identifiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_company_identifiers FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON crm_company_identifiers;
CREATE POLICY tenant_isolation ON crm_company_identifiers
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- ── Affiliations ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_affiliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL REFERENCES crm_tenants(id),
    person_id UUID NOT NULL REFERENCES crm_people(id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES crm_companies(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (btrim(role) <> ''),
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(attributes) = 'object'),
    is_active BOOLEAN NOT NULL DEFAULT true,
    since TIMESTAMPTZ NOT NULL DEFAULT now(),
    until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT crm_affiliations_key UNIQUE (tenant_id, person_id, company_id, role)
);

CREATE INDEX IF NOT EXISTS idx_crm_affiliations_company
    ON crm_affiliations (tenant_id, company_id, role);
CREATE INDEX IF NOT EXISTS idx_crm_affiliations_person
    ON crm_affiliations (person_id);

DROP TRIGGER IF EXISTS crm_affiliations_tenant_guard ON crm_affiliations;
CREATE TRIGGER crm_affiliations_tenant_guard
    BEFORE INSERT OR UPDATE ON crm_affiliations
    FOR EACH ROW EXECUTE FUNCTION crm_link_tenant_guard();

ALTER TABLE crm_affiliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_affiliations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON crm_affiliations;
CREATE POLICY tenant_isolation ON crm_affiliations
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS crm_affiliations;
--   DROP TABLE IF EXISTS crm_company_identifiers;
--   DROP FUNCTION IF EXISTS crm_link_tenant_guard();
--   DROP TRIGGER IF EXISTS crm_companies_parent_guard ON crm_companies;
--   DROP FUNCTION IF EXISTS crm_companies_parent_guard();
--   ALTER TABLE crm_companies DROP COLUMN IF EXISTS parent_company_id;
