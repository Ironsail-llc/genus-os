-- Migration 103: make benchmark_results.pass_rate mean what it says.
--
-- WHY. Since 063 the column named `pass_rate` has stored the *weighted
-- partial-credit aggregate* — the mean of per-task scores, where a task that
-- matched 1 of 2 required patterns contributes 0.5 — while `passed`/`failed`
-- on the same row counted cases against a 0.70 threshold. The two numbers
-- disagreed on 1956 of 2367 rows in this table. agent-architect's latest run
-- reported 0.5908 with 2 of 7 cases passed (0.2857). The Telegram /goals
-- command took the fraction from `passed`/`total_cases` and the percentage
-- from `pass_rate`, and printed crm-hygiene as "0/4 (18%)".
--
-- WHAT. Two columns, each honest about what it holds:
--
--   pass_rate        passed / total_cases  — "did the agent do its job?"
--   aggregate_score  the weighted partial-credit mean — useful for hill
--                    climbing (it moves when a task goes 0.4 -> 0.6), useless
--                    as a headline.
--
-- HISTORY IS RECOMPUTED, NOT RELABELLED. The backfill runs in two steps and
-- the order matters:
--
--   1. aggregate_score := pass_rate. The historical value IS the aggregate,
--      so this is a rename, and it is exact.
--   2. pass_rate := passed / total_cases. Both inputs are already stored on
--      every row, so the true rate is recoverable for the whole timeseries
--      rather than starting fresh from today. Step 2 must run after step 1 or
--      it destroys the aggregate before it has been copied.
--
-- judge_errors is added here too: every exception in the LLM-judge path used
-- to score 0.5, so a rate-limited judge was indistinguishable from a mediocre
-- agent. It is now counted as a failure and recorded, because a control you
-- cannot see the failures of is not a control.

BEGIN;

ALTER TABLE benchmark_results ADD COLUMN IF NOT EXISTS aggregate_score REAL;
ALTER TABLE benchmark_results ADD COLUMN IF NOT EXISTS judge_errors INTEGER NOT NULL DEFAULT 0;

-- Step 1 — rename in place. Guarded so a re-run cannot overwrite an
-- aggregate_score already written by the current handler.
UPDATE benchmark_results
   SET aggregate_score = pass_rate
 WHERE aggregate_score IS NULL;

-- Step 2 — recompute the headline. Idempotent: recomputing an already-correct
-- row yields the same value. Rows with no cases keep 0.0; a suite that graded
-- nothing is not a 100% pass.
UPDATE benchmark_results
   SET pass_rate = passed::real / total_cases
 WHERE total_cases > 0
   AND pass_rate IS DISTINCT FROM passed::real / total_cases;

UPDATE benchmark_results
   SET pass_rate = 0.0
 WHERE (total_cases IS NULL OR total_cases = 0)
   AND pass_rate <> 0.0;

COMMENT ON COLUMN benchmark_results.pass_rate IS
    'passed / total_cases. The canonical "did the agent do its job?" rate; '
    'feeds the benchmark_pass_rate goal metric. Recomputed for history by migration 103.';
COMMENT ON COLUMN benchmark_results.aggregate_score IS
    'Weighted partial-credit mean of per-task scores (0.0-1.0). What pass_rate '
    'held before migration 103. For hill climbing, never as a headline pass rate.';
COMMENT ON COLUMN benchmark_results.judge_errors IS
    'Cases whose LLM judge could not be evaluated (exception, empty or malformed '
    'response). Counted in failed, never scored as a neutral 0.5.';
COMMENT ON TABLE benchmark_results IS
    'Per-agent benchmark suite outcomes. pass_rate = passed/total_cases; '
    'aggregate_score = weighted partial credit. See migration 103.';

COMMIT;
