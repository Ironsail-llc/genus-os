# Benchmark Decontamination Runbook

Keeps benchmark-harness traffic out of every metric the operator is shown.

**Flags**: `ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED` +
`ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE`
(`robothor/engine/feature_flags.py::benchmark_decontamination_mode`).
Governed by the systemd drop-in — follow `GUARDRAIL_FLIPS.md` for the flip
mechanics; this runbook covers what each rung does and what to check.

## What was wrong

`benchmark_run` executes each suite task as a sub-agent run, but it called
`runner.execute(...)` with **no `SpawnContext`**, so every benchmark run
recorded `parent_run_id = NULL`. That is the exact shape `analytics.py` used —
in twelve hand-copied clauses — to mean "top-level production run".

Measured on this instance over 30 days of `agent_runs`:

| | runs | share |
|---|---|---|
| all runs | 4,267 | |
| benchmark runs | 2,685 | 63% |
| spend, all | $78.03 | |
| spend, benchmark | $29.93 | 38% |

Per agent (production = top-level, non-benchmark):

| agent | counted as production (before) | actually production | benchmark timeouts counted as the agent's |
|---|---|---|---|
| agent-architect | 189 | 19 | 44 of 44 |
| crm-dedup | 179 | 5 | 8 of 8 |
| curiosity-engine | 252 | 116 | 38 of 39 |
| email-analyst | 143 | **0** | 2 of 2 |
| auto-researcher | 216 | **0** | — |
| auto-agent | 120 | **0** | — |

Three agents have **zero** production runs. Their grade was computed entirely
over benchmark traffic.

The same missing spawn context let the runner's `auto_task` path file an
operator-facing CRM task per benchmark run: **6,887** rows titled
`<Agent>: sub_agent run` with body `trigger: benchmark:<suite>:<task>`, 1,179
of them in the last 30 days (49% of all tasks created in the window). Failed
and timed-out ones landed in the queue as `TODO`.

## The rungs

| mode | analytics numbers | reporting | benchmark sub-runs | CRM tasks |
|---|---|---|---|---|
| `off` (default) | legacy | none | unlinked (`parent_run_id NULL`) | suppressed¹ |
| `observe` | legacy | `benchmark_runs` / `benchmark_cost_usd` on every surface + a WARNING log | unlinked | suppressed¹ |
| `alert` | legacy | observe + operator notification (throttled to 1/hour/process) | unlinked | suppressed¹ |
| `enforce` | benchmark rows excluded | as above, plus `benchmark_excluded: true` | linked to the parent run | suppressed¹ |

¹ The CRM-task suppression is **not** gated. It is a hole in the *existing*
`is_benchmark` sandbox, not a new control: `tools/handlers/crm.py` already
refuses every task-mutating tool when `ctx.is_benchmark`, but the runner's
`auto_task` write goes straight to the DAL and never met that guard. See
`runner.should_create_auto_task`.

## Promotion procedure

One rung per 24h, per `GUARDRAIL_FLIPS.md`.

1. **observe** — confirm the measurement is real before changing any number:

   ```sh
   robothor engine ... # or in psql, on the live DB:
   SELECT agent_id,
          COUNT(*) FILTER (WHERE parent_run_id IS NULL) AS counted_as_production,
          COUNT(*) FILTER (WHERE parent_run_id IS NULL
                             AND trigger_detail LIKE 'benchmark:%') AS benchmark
     FROM agent_runs
    WHERE created_at > NOW() - INTERVAL '30 days'
    GROUP BY 1 ORDER BY 3 DESC;
   ```

   The `benchmark_runs` value reported by `get_agent_stats` must match the
   `benchmark` column for the same window.

2. **alert** — confirm one notification actually reaches the operator's
   channel (not just a DB row). If nothing arrives, do not promote further:
   the rung is not delivering.

3. **enforce** — after the flip, re-read the same agents. `total_runs` must
   drop to the `actually production` column above, and `benchmark_runs` must
   carry the remainder. Any agent whose entire footprint is benchmark traffic
   stays listed in `get_fleet_health` with `total_runs: 0` — deliberately, so
   "graded on nothing" is visible rather than silent.

## Probe (fire a real violation)

Do not trust a green test or an empty table. After enforcing:

1. Run one real suite against a benchmarked agent:
   `benchmark_run_for_agent(agent_id=<agent>, tag="probe-<date>")`.
2. Assert the new rows are linked, not orphaned:

   ```sql
   SELECT id, parent_run_id, trigger_detail
     FROM agent_runs
    WHERE trigger_detail LIKE 'benchmark:%'
      AND created_at > NOW() - INTERVAL '1 hour';
   -- every row must have a NON-NULL parent_run_id
   ```

3. Assert no CRM task was filed for it:

   ```sql
   SELECT COUNT(*) FROM crm_tasks
    WHERE body LIKE '%trigger: benchmark:%'
      AND created_at > NOW() - INTERVAL '1 hour';
   -- must be 0
   ```

4. Assert the operator surface moved: `GET /costs` must now report
   `benchmark_cost_usd` separately from each agent's `total_cost_usd`.

## Rollback

Set `ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE=observe` (or
`..._ENABLED=0`) and restart. Nothing is destructive — no rows are rewritten,
the filter is read-path only, and historical benchmark rows are matched by
`trigger_detail`, so a rollback restores the old numbers exactly.

## Historical rows

Linking future sub-runs cannot fix the 2,685 rows already written with a NULL
parent. The `trigger_detail LIKE 'benchmark:%'` clause is what cleans those,
which is why it stays in the filter permanently rather than being a migration.
