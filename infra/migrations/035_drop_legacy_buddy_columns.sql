-- Migration 035: Drop legacy RPG columns from buddy_stats + agent_buddy_stats
--
-- Prerequisite: migration 034 has been applied and agent_buddy_stats.achievement_score
-- has been populated for at least 30 days of rows. Verify by running:
--     SELECT COUNT(DISTINCT stat_date) FROM agent_buddy_stats WHERE achievement_score IS NOT NULL;
-- Expect >= 30.
--
-- This migration removes:
--   - XP/level/streak-longest columns (gamification scrap)
--   - debugging_score, patience_score, chaos_score, wisdom_score
--   - effectiveness_score, benchmark_dim_score, reliability_score
--   - overall_score (superseded by achievement_score)
--   - daily activity counters (tasks_completed stays — used by streak display)
--
-- Kept:
--   - stat_date, agent_id, achievement_score
--   - tasks_completed (still useful for UI)
--   - current_streak_days, longest_streak_days (cosmetic streak display)
--   - computed_at

BEGIN;

-- ══════════════════════════════════════════════════════════════════
-- agent_buddy_stats — drop per-agent RPG columns
-- ══════════════════════════════════════════════════════════════════

ALTER TABLE agent_buddy_stats
    DROP COLUMN IF EXISTS debugging_score,
    DROP COLUMN IF EXISTS patience_score,
    DROP COLUMN IF EXISTS chaos_score,
    DROP COLUMN IF EXISTS wisdom_score,
    DROP COLUMN IF EXISTS reliability_score,
    DROP COLUMN IF EXISTS effectiveness_score,
    DROP COLUMN IF EXISTS benchmark_dim_score,
    DROP COLUMN IF EXISTS overall_score,
    DROP COLUMN IF EXISTS daily_xp,
    DROP COLUMN IF EXISTS total_xp,
    DROP COLUMN IF EXISTS level,
    DROP COLUMN IF EXISTS errors_recovered,
    DROP COLUMN IF EXISTS last_benchmark_score,
    DROP COLUMN IF EXISTS last_benchmark_at;

-- ══════════════════════════════════════════════════════════════════
-- buddy_stats — drop fleet-level RPG columns
-- ══════════════════════════════════════════════════════════════════

ALTER TABLE buddy_stats
    DROP COLUMN IF EXISTS debugging_score,
    DROP COLUMN IF EXISTS patience_score,
    DROP COLUMN IF EXISTS chaos_score,
    DROP COLUMN IF EXISTS wisdom_score,
    DROP COLUMN IF EXISTS reliability_score,
    DROP COLUMN IF EXISTS effectiveness_score,
    DROP COLUMN IF EXISTS benchmark_dim_score,
    DROP COLUMN IF EXISTS total_xp,
    DROP COLUMN IF EXISTS level,
    DROP COLUMN IF EXISTS emails_processed,
    DROP COLUMN IF EXISTS insights_generated,
    DROP COLUMN IF EXISTS errors_avoided,
    DROP COLUMN IF EXISTS dreams_completed;

COMMIT;
