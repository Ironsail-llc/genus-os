-- Goal reports aggregate only unresolved and independently confirmed effects.
CREATE INDEX IF NOT EXISTS agent_runtime_effects_goal_evidence
    ON agent_runtime_effects(tenant_id,goal_id,state)
    WHERE state IN ('prepared','dispatching','uncertain','confirmed');
