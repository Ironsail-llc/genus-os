-- Federation tables — peer-to-peer instance networking
-- Migration: 025_federation.sql

-- This instance's identity
CREATE TABLE IF NOT EXISTS federation_identity (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    private_key_ref TEXT NOT NULL,  -- reference to vault/SOPS, not the key itself
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Connections to other instances
CREATE TABLE IF NOT EXISTS federation_connections (
    id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    peer_name TEXT NOT NULL,
    peer_endpoint TEXT NOT NULL,
    peer_public_key TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'peer',       -- parent, child, peer
    state TEXT NOT NULL DEFAULT 'pending',            -- pending, active, limited, suspended
    exports JSONB NOT NULL DEFAULT '[]',
    imports JSONB NOT NULL DEFAULT '[]',
    nats_account TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Event journal (sync buffer)
CREATE TABLE IF NOT EXISTS federation_events (
    id BIGSERIAL PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES federation_connections(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,           -- critical, bulk, media
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    hlc_timestamp TEXT NOT NULL,     -- hybrid logical clock
    synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fed_events_conn_channel
    ON federation_events(connection_id, channel, id);

CREATE INDEX IF NOT EXISTS idx_fed_events_unsynced
    ON federation_events(connection_id, channel)
    WHERE synced_at IS NULL;

-- Add FEDERATION to trigger_type CHECK if it doesn't already include it
DO $$
BEGIN
    -- Check if the constraint exists and needs updating
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_runs_trigger_type_check'
        AND conrelid = 'agent_runs'::regclass
    ) THEN
        ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_trigger_type_check;
        ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_trigger_type_check
            CHECK (trigger_type IN (
                'cron', 'hook', 'event', 'manual', 'telegram', 'webchat',
                'workflow', 'sub_agent', 'federation'
            ));
    END IF;
EXCEPTION WHEN undefined_table THEN
    -- agent_runs table may not exist on fresh installs with different migration order
    NULL;
END $$;
