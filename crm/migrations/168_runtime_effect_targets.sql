-- What an effect acted on, so an unresolved write fences a repeat of the same
-- action (same tool, same target) rather than every later call of the tool.
-- `target` is a short, redacted summary of the target arguments (never the
-- wording); `target_key` is its hash, which the fence compares. Rows written
-- before this migration have neither, and keep fencing the whole tool.
ALTER TABLE agent_runtime_effects ADD COLUMN IF NOT EXISTS target JSONB;
ALTER TABLE agent_runtime_effects ADD COLUMN IF NOT EXISTS target_key TEXT;
CREATE INDEX IF NOT EXISTS agent_runtime_effects_uncertain_target
    ON agent_runtime_effects(tenant_id,principal_id,tool_name,target_key)
    WHERE state='uncertain';
