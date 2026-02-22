-- Telemetry table for time-series service health metrics
-- Database: robothor_memory
-- Run: psql robothor_memory -f 004_telemetry_table.sql

BEGIN;

CREATE TABLE IF NOT EXISTS telemetry (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    service VARCHAR(100) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit VARCHAR(30),
    details JSONB
);

CREATE INDEX IF NOT EXISTS idx_telemetry_service ON telemetry (service);
CREATE INDEX IF NOT EXISTS idx_telemetry_metric ON telemetry (metric);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_service_metric ON telemetry (service, metric, timestamp DESC);

-- Add session_key column to audit_log if it doesn't exist
-- (existing audit_log may not have this column)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'audit_log' AND column_name = 'session_key'
    ) THEN
        ALTER TABLE audit_log ADD COLUMN session_key VARCHAR(255);
    END IF;
END $$;

-- Retention policy: auto-delete telemetry older than 90 days (run via cron)
-- DELETE FROM telemetry WHERE timestamp < NOW() - INTERVAL '90 days';

COMMIT;
