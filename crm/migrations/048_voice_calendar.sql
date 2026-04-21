-- ─────────────────────────────────────────────────────────────────────────────
-- 048_voice_calendar.sql
--
-- Phase 1c of the Unified Person Graph. First-class storage for voice calls
-- and calendar events. Both channels were previously not stored at all in
-- the CRM (Twilio call logs vanished; calendar lived only in Google).
--
-- Three new tables:
--   - call_log                       voice call (and we reuse for sms? no — sms goes through `message`)
--   - calendar_event                 google/outlook calendar event shadow
--   - calendar_event_participant     junction (event × role × person)
--
-- Calls keep a direct person_id (calls are almost always 1:1; saves a join).
-- Calendar events use a participant junction (multi-attendee meetings).
--
-- Rollback:
--   DROP TABLE IF EXISTS calendar_event_participant;
--   DROP TABLE IF EXISTS calendar_event;
--   DROP TABLE IF EXISTS call_log;
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── call_log ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    connected_account_id UUID REFERENCES connected_account(id) ON DELETE SET NULL,
    person_id UUID REFERENCES crm_people(id) ON DELETE SET NULL,

    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    from_number TEXT,
    to_number TEXT,

    status TEXT NOT NULL DEFAULT 'initiated' CHECK (status IN (
        'initiated', 'ringing', 'in_progress', 'completed',
        'busy', 'no_answer', 'failed', 'canceled'
    )),
    duration_ms INTEGER,

    twilio_call_sid TEXT UNIQUE,
    recording_url TEXT,
    transcript_text TEXT,
    ai_summary TEXT,

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,

    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_call_log_person
    ON call_log(tenant_id, person_id, started_at DESC)
    WHERE person_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_call_log_recent
    ON call_log(tenant_id, started_at DESC);


-- ── calendar_event ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calendar_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    connected_account_id UUID REFERENCES connected_account(id) ON DELETE SET NULL,

    google_event_id TEXT,              -- nullable so we can support outlook later
    calendar_id TEXT,                  -- e.g. 'user@example.com'
    title TEXT,
    description TEXT,
    location TEXT,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,

    organizer_email TEXT,
    hangout_link TEXT,
    status TEXT,                        -- 'confirmed' | 'tentative' | 'cancelled'
    recurrence_rule TEXT,               -- RRULE for recurring events

    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, google_event_id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_event_recent
    ON calendar_event(tenant_id, start_at DESC NULLS LAST);


-- ── calendar_event_participant ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calendar_event_participant (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    event_id UUID NOT NULL REFERENCES calendar_event(id) ON DELETE CASCADE,
    person_id UUID REFERENCES crm_people(id) ON DELETE SET NULL,

    role TEXT NOT NULL DEFAULT 'attendee' CHECK (role IN ('organizer', 'attendee', 'optional', 'resource')),
    email TEXT NOT NULL,
    display_name TEXT,
    response_status TEXT,              -- 'accepted' | 'declined' | 'tentative' | 'needsAction'
    organizer BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calendar_event_participant_person
    ON calendar_event_participant(person_id, event_id)
    WHERE person_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_calendar_event_participant_event
    ON calendar_event_participant(event_id);

COMMIT;
