-- Migration 027: KAIROS autoDream + Buddy gamification
-- Adds: autodream_runs table, buddy_stats table, buddy_profile table, new memory blocks.
-- Part of Conway + KAIROS + Buddy feature upgrade.

BEGIN;

-- ══════════════════════════════════════════════════════════════════════════════
-- KAIROS — autoDream run tracking
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS autodream_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mode TEXT NOT NULL CHECK (mode IN ('idle', 'post_stall', 'scheduled', 'deep')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    facts_consolidated INTEGER DEFAULT 0,
    facts_pruned INTEGER DEFAULT 0,
    insights_discovered INTEGER DEFAULT 0,
    importance_scores_updated INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_autodream_started ON autodream_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_autodream_mode ON autodream_runs (mode);

-- ══════════════════════════════════════════════════════════════════════════════
-- BUDDY — Gamification data layer
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS buddy_stats (
    stat_date DATE PRIMARY KEY DEFAULT CURRENT_DATE,
    -- Raw counters (incremented throughout the day)
    tasks_completed INTEGER DEFAULT 0,
    emails_processed INTEGER DEFAULT 0,
    insights_generated INTEGER DEFAULT 0,
    errors_avoided INTEGER DEFAULT 0,
    dreams_completed INTEGER DEFAULT 0,
    -- Computed scores (refreshed daily by buddy.refresh_daily())
    debugging_score INTEGER DEFAULT 50,
    patience_score INTEGER DEFAULT 50,
    chaos_score INTEGER DEFAULT 50,
    wisdom_score INTEGER DEFAULT 50,
    reliability_score INTEGER DEFAULT 50,
    -- XP and progression
    total_xp BIGINT DEFAULT 0,
    level INTEGER DEFAULT 1,
    -- Streaks
    current_streak_days INTEGER DEFAULT 0,
    longest_streak_days INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS buddy_profile (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    name TEXT DEFAULT 'Robothor',
    species TEXT DEFAULT 'phoenix',
    total_xp BIGINT DEFAULT 0,
    level INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed the singleton profile
INSERT INTO buddy_profile (id) VALUES (1) ON CONFLICT DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- New memory blocks
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO agent_memory_blocks (block_name, block_type, max_chars) VALUES
    ('autodream_log', 'persistent', 5000),
    ('buddy_status', 'persistent', 2000)
ON CONFLICT (block_name) DO NOTHING;

COMMIT;
