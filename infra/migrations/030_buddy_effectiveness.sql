-- Migration 030: Buddy goal-oriented scoring dimensions
-- Adds effectiveness_score (outcome quality) and benchmark_dim_score
-- to both agent_buddy_stats and buddy_stats tables.
-- These replace chaos and wisdom as active scoring dimensions.

BEGIN;

-- ══════════════════════════════════════════════════════════════════
-- Per-agent stats — new scoring dimensions
-- ══════════════════════════════════════════════════════════════════

ALTER TABLE agent_buddy_stats
    ADD COLUMN IF NOT EXISTS effectiveness_score INTEGER DEFAULT 50;

ALTER TABLE agent_buddy_stats
    ADD COLUMN IF NOT EXISTS benchmark_dim_score INTEGER DEFAULT 50;

-- ══════════════════════════════════════════════════════════════════
-- Global stats — new scoring dimensions
-- ══════════════════════════════════════════════════════════════════

ALTER TABLE buddy_stats
    ADD COLUMN IF NOT EXISTS effectiveness_score INTEGER DEFAULT 50;

ALTER TABLE buddy_stats
    ADD COLUMN IF NOT EXISTS benchmark_dim_score INTEGER DEFAULT 50;

COMMIT;
