-- Migration 148: provider-neutral calendar_event identity.
--
-- calendar_event was keyed by UNIQUE (tenant_id, google_event_id), which leaves
-- no place for a Microsoft 365 (Graph) event. Events are now identified by
-- (tenant_id, provider, external_event_id); the gws write-through
-- (robothor/engine/tools/handlers/gws.py `_record_calendar_event`) upserts on
-- that key.
--
-- Dual-write: google_event_id and its unique constraint stay. Google events
-- keep writing both columns, so existing readers and the old constraint keep
-- working; non-Google events leave google_event_id NULL.
--
-- Row-level security on calendar_event is per-row on tenant_id (081 backstop)
-- and grants are table-level, so the new columns need no policy or grant.
ALTER TABLE calendar_event ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'google';
ALTER TABLE calendar_event ADD COLUMN IF NOT EXISTS external_event_id TEXT;

-- Every row before 148 came from Google.
UPDATE calendar_event
   SET external_event_id = google_event_id
 WHERE external_event_id IS NULL
   AND google_event_id IS NOT NULL;

-- Writers that predate 148 (an old engine still running during a rolling
-- deploy) set only google_event_id. Fill the new key from it so the row is
-- found by the new upsert instead of tripping the old unique constraint.
CREATE OR REPLACE FUNCTION calendar_event_fill_external_id() RETURNS trigger AS $$
BEGIN
    IF NEW.external_event_id IS NULL AND NEW.google_event_id IS NOT NULL
       AND NEW.provider = 'google' THEN
        NEW.external_event_id := NEW.google_event_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_calendar_event_fill_external_id ON calendar_event;
CREATE TRIGGER trg_calendar_event_fill_external_id
    BEFORE INSERT OR UPDATE ON calendar_event
    FOR EACH ROW EXECUTE FUNCTION calendar_event_fill_external_id();

CREATE UNIQUE INDEX IF NOT EXISTS uq_calendar_event_provider_external_id
    ON calendar_event (tenant_id, provider, external_event_id);

-- Rollback:
--   DROP TRIGGER IF EXISTS trg_calendar_event_fill_external_id ON calendar_event;
--   DROP FUNCTION IF EXISTS calendar_event_fill_external_id();
--   DROP INDEX IF EXISTS uq_calendar_event_provider_external_id;
--   ALTER TABLE calendar_event DROP COLUMN IF EXISTS external_event_id,
--       DROP COLUMN IF EXISTS provider;
