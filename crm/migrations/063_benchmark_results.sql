-- 063_benchmark_results.sql
-- 2026-05-06 — benchmark grading: every agent's job grade lives here.
--
-- Background: docs/benchmarks/<agent>/suite.yaml describes each agent's
-- job as input → expected-output pairs. Until now those suites had no
-- canonical place to land their scores — results lived only in memory
-- blocks (benchmark_run:<suite>:<tag>), which can't be queried as a
-- timeseries and aren't joinable with experiments or agent_runs.
--
-- This table is the source of truth for "did the agent do its job?"
-- and feeds the new `benchmark_pass_rate` goal metric (goals.py).
-- Cost is captured but never targeted as an optimization metric.

CREATE TABLE IF NOT EXISTS benchmark_results (
  id              SERIAL PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  suite_id        TEXT NOT NULL,                       -- matches docs/benchmarks/<agent>/suite.yaml `id:` field
  suite_path      TEXT,                                -- relative path of suite source, for traceability
  run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  total_cases     INTEGER NOT NULL,
  passed          INTEGER NOT NULL,
  failed          INTEGER NOT NULL,
  pass_rate       REAL    NOT NULL,                    -- weighted aggregate score, 0.0-1.0
  category_scores JSONB   NOT NULL DEFAULT '{}'::jsonb, -- {correctness: 0.85, safety: 1.0, ...}
  failures        JSONB   NOT NULL DEFAULT '[]'::jsonb, -- [{case_id, score, why_failed?}]
  triggered_by    TEXT    NOT NULL,                    -- 'cron' | 'auto-researcher:before' | 'auto-researcher:after' | 'manual'
  experiment_id   TEXT,                                -- optional link to docs/experiments/<id>.yaml
  cost_usd        REAL,                                -- observed, never targeted
  duration_ms     INTEGER,
  tenant_id       TEXT NOT NULL DEFAULT 'robothor-primary'
);

CREATE INDEX IF NOT EXISTS idx_benchmark_results_agent_run_at
  ON benchmark_results (agent_id, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_benchmark_results_experiment
  ON benchmark_results (experiment_id)
  WHERE experiment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_benchmark_results_triggered_by
  ON benchmark_results (triggered_by, run_at DESC);

COMMENT ON TABLE benchmark_results IS
  'Per-agent benchmark suite outcomes. The `pass_rate` column is the canonical "did the agent do its job?" metric and feeds the benchmark_pass_rate goal.';

COMMENT ON COLUMN benchmark_results.cost_usd IS
  'Observed run cost. Never used as an optimization target — see plan 2026-05-06.';
