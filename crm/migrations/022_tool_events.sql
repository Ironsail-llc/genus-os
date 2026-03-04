-- Tool invocation tracking for observability.
-- Records every tool call with timing, success/failure, and error type.

CREATE TABLE IF NOT EXISTS agent_tool_events (
    id SERIAL PRIMARY KEY,
    run_id UUID REFERENCES agent_runs(id),
    step_id UUID,
    tool_name TEXT NOT NULL,
    duration_ms INTEGER,
    success BOOLEAN NOT NULL,
    error_type TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_events_run ON agent_tool_events(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_events_tool ON agent_tool_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_events_created ON agent_tool_events(created_at);
