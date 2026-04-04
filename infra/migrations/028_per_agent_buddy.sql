-- Migration 028: Per-agent RPG scoring for Buddy system
-- Adds agent_buddy_stats table for per-agent gamification (scores, XP, levels, ranking).
-- The global buddy_stats/buddy_profile tables remain for the "team level" aggregate.

BEGIN;

-- ══════════════════════════════════════════════════════════════════════════════
-- Per-agent Buddy stats — one row per agent per day
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_buddy_stats (
    agent_id          TEXT NOT NULL,
    stat_date         DATE NOT NULL DEFAULT CURRENT_DATE,
    -- Raw counters
    tasks_completed   INTEGER DEFAULT 0,
    errors_recovered  INTEGER DEFAULT 0,
    -- RPG scores (0-100)
    debugging_score   INTEGER DEFAULT 50,
    patience_score    INTEGER DEFAULT 50,
    chaos_score       INTEGER DEFAULT 50,
    wisdom_score      INTEGER DEFAULT 50,
    reliability_score INTEGER DEFAULT 50,
    -- Weighted composite of the 5 scores
    overall_score     INTEGER DEFAULT 50,
    -- XP and leveling
    daily_xp          INTEGER DEFAULT 0,
    total_xp          BIGINT DEFAULT 0,
    level             INTEGER DEFAULT 1,
    -- Benchmark integration (written by AutoAgent benchmark_run)
    last_benchmark_score NUMERIC(5,3),
    last_benchmark_at    TIMESTAMPTZ,
    -- Metadata
    computed_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_id, stat_date)
);

-- Fast lookup: leaderboard (today's scores sorted by overall)
CREATE INDEX IF NOT EXISTS idx_agent_buddy_overall
    ON agent_buddy_stats (stat_date, overall_score DESC);

-- Fast lookup: single agent history
CREATE INDEX IF NOT EXISTS idx_agent_buddy_agent
    ON agent_buddy_stats (agent_id, stat_date DESC);

COMMIT;
