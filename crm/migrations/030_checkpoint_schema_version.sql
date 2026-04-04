-- Add schema_version to checkpoints for safe resume across code changes
ALTER TABLE agent_run_checkpoints
    ADD COLUMN IF NOT EXISTS schema_version integer DEFAULT 0;
