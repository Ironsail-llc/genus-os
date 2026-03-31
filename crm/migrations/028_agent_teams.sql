-- Agent Teams: persistent team definitions and message audit trail
-- Teams are primarily managed in Redis for real-time access, but this table
-- provides durable storage and audit history.

CREATE TABLE IF NOT EXISTS agent_teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL DEFAULT '',
    member_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dissolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_teams_team_id ON agent_teams (team_id);

CREATE TABLE IF NOT EXISTS agent_team_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id TEXT NOT NULL DEFAULT '',
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_team_messages_team_id ON agent_team_messages (team_id);
CREATE INDEX IF NOT EXISTS idx_team_messages_from ON agent_team_messages (from_agent);
CREATE INDEX IF NOT EXISTS idx_team_messages_created ON agent_team_messages (created_at);
