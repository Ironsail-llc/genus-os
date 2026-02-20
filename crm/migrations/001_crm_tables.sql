-- CRM Consolidation: Native PostgreSQL tables replacing Twenty CRM + Chatwoot
-- Database: robothor_memory
-- Run: psql robothor_memory -f 001_crm_tables.sql
--
-- Preserves original IDs:
--   - UUID primary keys for Twenty-origin tables (companies, people, notes, tasks)
--   - SERIAL integers for Chatwoot-origin tables (conversations, messages)

BEGIN;

-- ─── Companies ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_companies (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    domain_name TEXT,
    employees INTEGER,
    address_street1 TEXT,
    address_street2 TEXT,
    address_city TEXT,
    address_state TEXT,
    address_postcode TEXT,
    address_country TEXT,
    linkedin_url TEXT,
    annual_recurring_revenue_micros BIGINT,
    annual_recurring_revenue_currency TEXT,
    ideal_customer_profile BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_companies_name ON crm_companies (name);
CREATE INDEX IF NOT EXISTS idx_crm_companies_updated_at ON crm_companies (updated_at);

-- ─── People ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_people (
    id UUID PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    additional_emails JSONB,
    phone TEXT,
    phone_country_code TEXT,
    phone_calling_code TEXT,
    additional_phones JSONB,
    job_title TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT,
    x_url TEXT,
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_people_name ON crm_people (first_name, last_name);
CREATE INDEX IF NOT EXISTS idx_crm_people_email ON crm_people (email);
CREATE INDEX IF NOT EXISTS idx_crm_people_updated_at ON crm_people (updated_at);
CREATE INDEX IF NOT EXISTS idx_crm_people_company_id ON crm_people (company_id);

-- ─── Notes ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_notes (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_notes_person_id ON crm_notes (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_notes_company_id ON crm_notes (company_id);
CREATE INDEX IF NOT EXISTS idx_crm_notes_updated_at ON crm_notes (updated_at);

-- ─── Tasks ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_tasks (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    status TEXT,
    due_at TIMESTAMPTZ,
    assignee_id UUID,
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_tasks_status ON crm_tasks (status);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_person_id ON crm_tasks (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_updated_at ON crm_tasks (updated_at);

-- ─── Conversations ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_conversations (
    id SERIAL PRIMARY KEY,
    person_id UUID REFERENCES crm_people(id),
    status TEXT NOT NULL DEFAULT 'open',
    inbox_name TEXT NOT NULL DEFAULT '',
    messages_count INTEGER NOT NULL DEFAULT 0,
    last_activity_at TIMESTAMPTZ,
    additional_attributes JSONB,
    custom_attributes JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_conversations_status ON crm_conversations (status);
CREATE INDEX IF NOT EXISTS idx_crm_conversations_person_id ON crm_conversations (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_conversations_last_activity ON crm_conversations (last_activity_at);

-- ─── Messages ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES crm_conversations(id),
    content TEXT,
    message_type TEXT NOT NULL DEFAULT 'incoming',
    private BOOLEAN NOT NULL DEFAULT FALSE,
    sender_name TEXT,
    sender_type TEXT,
    content_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_messages_conversation_id ON crm_messages (conversation_id);
CREATE INDEX IF NOT EXISTS idx_crm_messages_created_at ON crm_messages (created_at);

-- ─── Add person_id column to contact_identifiers ───────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'contact_identifiers' AND column_name = 'person_id'
    ) THEN
        ALTER TABLE contact_identifiers ADD COLUMN person_id UUID;
    END IF;
END $$;

COMMIT;
