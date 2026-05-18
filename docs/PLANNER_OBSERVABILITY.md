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
| `planner.run_complete` | INFO | `plan_all_stalled` end | `tenant_id`, `candidates_count`, `actions` (dict of action → count), `elapsed_ms`, `dry_run` | One per beat. Roll up to a time series for "is the planner running?" and "what is it deciding?" |
| `planner.action.refused` | WARNING | `apply_plan` when `verdict == "refuse"` | `task_id`, `rationale`, `action_type` | Refusals are the leading indicator that an autonomy budget is too strict or an objective veto is firing unexpectedly. |
| `todo_promotion.created` | INFO | `todo_promotion.promote_todo_to_subtask` (Phase 3) | `parent_task_id`, `subtask_id`, `content_hash`, `agent_id`, `run_id` | Track how often unfinished todos turn into real subtasks. |
| `todo_promotion.skipped` | DEBUG | `todo_promotion.should_promote` returns False | `parent_task_id`, `reason` | Diagnose cycle-guard / manifest-opt-out / env-disabled / cap-exceeded skips. |
| `question.answered` | INFO | `dal.answer_question` (Phase 4) | `task_id`, `by`, `escalation_count_was`, `advance_to` | Pair with `planner.action.refused` rate to measure operator-feedback loop closure. |
| `checkpoint.resume.todo` | INFO if items > 0 else DEBUG | `runner.execute` resume path (Phase 5) | `run_id`, `items_count` | Confirm TodoList survives checkpoint round-trips after the Phase-5 fix. |

## Prometheus metrics

Defined in `robothor.engine.metrics`. Scraped via the `/metrics` endpoint
registered in `health.py`.

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `robothor_planner_actions_total` | Counter | `action` (execute/ask/wait/close), `tenant` | Increment per `apply_plan` call. Use `rate()` to graph planner velocity. |
| `robothor_planner_run_duration_seconds` | Histogram | `tenant` | Wall-clock per beat. Alert if p95 > 5s — usually a sign the candidate query is missing an index. |
| `robothor_todo_promotions_total` | Counter | `agent`, `outcome` (Phase 3) | `outcome` ∈ {created, idempotent, skipped}. Alert on a steep climb in `skipped{reason="cycle_guard"}`. |
| `robothor_task_questions_answered_total` | Counter | `tenant`, `advance_to` (Phase 4) | Should track 1:1 with `planner.action.refused` over the long run. |

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
