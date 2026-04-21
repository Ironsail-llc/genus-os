-- Migration 034: Buddy adopts goals.py as the scoring source of truth
--
-- Adds a single `achievement_score` column (0-100 integer) to both
-- buddy_stats and agent_buddy_stats. Populated by refresh_daily() from
-- `compute_achievement_score()` in robothor/engine/goals.py — a weighted
-- 0.0–1.0 rollup of how many declared goals each agent is hitting, scaled
-- to 0-100.
--
-- The legacy RPG columns (debugging_score, patience_score, chaos_score,
-- wisdom_score, effectiveness_score, benchmark_dim_score, reliability_score,
-- overall_score, daily_xp, total_xp, level) stay in place for backwards
-- compatibility during the soak period. Migration 035 drops them after
-- 30 days of observation on the new scoring.

BEGIN;

ALTER TABLE agent_buddy_stats
    ADD COLUMN IF NOT EXISTS achievement_score INTEGER;

ALTER TABLE buddy_stats
    ADD COLUMN IF NOT EXISTS achievement_score INTEGER;

COMMENT ON COLUMN agent_buddy_stats.achievement_score IS
    '0-100 weighted goal-achievement score from compute_achievement_score() '
    'in robothor/engine/goals.py. Supersedes overall_score as of 2026-04-18.';

COMMENT ON COLUMN buddy_stats.achievement_score IS
    '0-100 fleet-wide average of per-agent achievement scores. '
    'Supersedes overall_score as of 2026-04-18.';

COMMIT;
