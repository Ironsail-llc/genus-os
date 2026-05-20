# Thread Planner Observability

Phase 2 instruments `robothor.engine.thread_planner` with structured logs,
Prometheus counters, and a per-beat histogram. This page documents what
emits where so operators can build dashboards and alerts without reading
the source.

## Structured log events

All events are emitted via `logging` with an `extra={"event": "<name>", ...}`
payload — log aggregators that parse JSON pick up the structured fields.
`event` is the discriminator.

| Event | Level | Where | Fields | Why |
|---|---|---|---|---|
| `planner.run_complete` | INFO | `plan_all_stalled` end (always — including candidate-load failure) | `tenant_id`, `candidates_count`, `actions` (dict of action → count), `elapsed_ms`, `dry_run`, optional `error` | One per beat. Roll up to a time series for "is the planner running?" and "what is it deciding?". On candidate-load failure, `candidates_count=0`, `actions={}`, `error=repr(exc)` — distinguish DB outage from "no work this beat". |
| `planner.action.refused` | WARNING | `apply_plan` when `verdict == "refuse"` | `task_id`, `rationale`, `action_type` | Refusals are the leading indicator that an autonomy budget is too strict or an objective veto is firing unexpectedly. |

## Prometheus metrics

Defined in `robothor.engine.metrics`. Scraped via the `/metrics` endpoint
registered in `health.py`.

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `robothor_planner_actions_total` | Counter | `action` (execute/ask/wait/close), `tenant` | Incremented at action **dispatch** in `apply_plan`, before the DAL write. An `execute` plan missing `next_action`, or a `dal.set_next_action` returning `False`, still bumps the counter — so this is "attempted decisions per beat," not "successful writes." Pair with `task.created` / `task.updated` events if you need write-success rates. Use `rate()` to graph planner velocity. |
| `robothor_planner_run_duration_seconds` | Histogram | `tenant` | Wall-clock per beat, observed in the `finally` block — even on candidate-load failure. Alert if p95 > 5s — usually a sign the candidate query is missing an index. |

## Useful queries

### "Planner is alive and producing"

```promql
sum(rate(robothor_planner_actions_total[5m])) by (action)
```

A flat zero on this graph means the planner isn't being driven by the
warmup hook — check that `main` is running on cron AND that
`ROBOTHOR_PLANNER_ENABLED` is not `0`.

### "What is the planner asking about?"

```sql
SELECT
  metadata->>'question' AS question,
  COUNT(*) AS times_asked,
  MAX(created_at) AS last_asked
FROM crm_task_history
WHERE metadata->>'kind' = 'ask'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY metadata->>'question'
ORDER BY times_asked DESC
LIMIT 20;
```

Top repeated questions are usually a missing heuristic — operators
answer the same thing over and over until the planner learns.

### "Planner beat latency"

```promql
histogram_quantile(0.95, sum(rate(robothor_planner_run_duration_seconds_bucket[5m])) by (le, tenant))
```

p95 normally under 200ms. Spikes correlate with `_load_planner_candidates`
queries that need an index refresh.

## Disabling

Set `ROBOTHOR_PLANNER_ENABLED=0` in the engine's systemd unit env and
restart `robothor-engine`. Phase 2 changed the default from off to on —
this is the explicit opt-out path. The structured log
`planner.run_complete` won't fire when the planner is off.
