# Agent Goal Taxonomy

Every agent in Robothor has an explicit **goals** contract in its YAML manifest. Goals are the primary signal the self-improvement loop uses to decide what to fix. When a goal breaches persistently, the `buddy` agent selects a corrective-action template (see `docs/agents/corrective-actions.yaml`), grounds it in recent agent_reviews evidence, and queues a self-improve task for `auto-agent`. The fix is then verified by `buddy-grader` 48h after it ships.

This file is the shared reference. Every manifest must follow it.

## The four categories

Goals fall into exactly one of these categories. Each category maps to a remediation class — the kind of change the self-improvement loop proposes when the goal breaches.

| Category        | What it measures                                             | When breached, the loop typically changes…                                       |
| --------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------- |
| **reach**       | Did the output get to the intended recipient?                 | Delivery channel config, bot token, routing rules, fallback channel.              |
| **quality**     | Is the output substantive and accurate?                       | Instruction prompt, warmup context, tool selection, model tier, required sections. |
| **efficiency**  | Did it minimize unnecessary iteration or tool noise?          | `max_iterations`, prompt size, tool pruning, model tier. Cost and duration are **tracked**, never capped — see Observability below. |
| **correctness** | Did the run complete without errors or wrong outcomes?        | Tool implementation, guardrail config, flow restructure, schema validation.       |

## Standard metric vocabulary

Use these metric names consistently so cross-agent analytics work:

| Metric                                | Category     | Computed from                                              |
| ------------------------------------- | ------------ | ---------------------------------------------------------- |
| `delivery_success_rate`               | reach        | `delivery_status='delivered'` / runs with announce mode    |
| `inbox_read_rate`                     | reach        | unread notifications acknowledged within SLA               |
| `min_output_chars`                    | quality      | median `char_length(output_text)` over window              |
| `required_sections_present`           | quality      | fraction of outputs containing all declared sections       |
| `operator_rating_avg`                 | quality      | `agent_reviews` rating avg (`reviewer_type='operator'`)    |
| `substantive_output_rate`             | quality      | fraction where `char_length(output_text) >= min_output`    |
| `error_rate`                          | correctness  | `status='failed'` / total runs                             |
| `tool_success_rate`                   | correctness  | fraction of tool_call steps with no `error_message`        |
| `recovery_rate`                       | correctness  | fraction of runs where an error step was followed by a non-error step in the same run |
| `task_completion_rate`                | correctness  | resolved tasks / created tasks                             |
| `experiment_measure_success_rate`     | correctness  | experiment_measure calls without error (auto-researcher)   |
| `pr_merge_rate`                       | quality      | merged PRs / created PRs (Nightwatch)                      |
| `pr_revert_rate`                      | correctness  | reverted-after-merge PRs / merged PRs                      |

## Observability metrics — tracked, never targeted

These are written to `agent_runs` on every run and surfaced in
dashboards / reports. They are **not** goals. We do not set targets
for them, and the self-improvement loop does not act on them alone.
If cost or duration spikes unexpectedly, an operator looks and
decides; no automatic cap ever fires.

| Metric                     | Computed from                                             |
| -------------------------- | --------------------------------------------------------- |
| `avg_duration_ms`          | mean `duration_ms` over window                            |
| `p95_duration_ms`          | 95th percentile `duration_ms`                             |
| `avg_cost_usd`             | mean `total_cost_usd`                                     |
| `p95_cost_usd`             | 95th percentile `total_cost_usd`                          |
| `total_cost_usd_window`    | sum of `total_cost_usd` over window (spend tracking)      |
| `timeout_rate`             | `status='timeout'` / total runs (should be ~0; non-zero means a genuine provider hang caught by an opt-in stall watchdog) |
| `input_tokens_avg`         | mean `input_tokens` (context size diagnostic)             |

If you want any of these *investigated* (not enforced), point the
buddy review pass at them via a standing note, not a goal target.

## The goals block shape

```yaml
goals:
  reach:
    - {id: <short-id>, metric: <metric-name>, target: "<comparison>", weight: <float>, window_days: <int>}
  quality:
    - ...
  efficiency:
    - ...
  correctness:
    - ...
```

**Fields:**
- `id` — short human-readable slug, unique within the manifest.
- `metric` — name from the vocabulary above.
- `target` — comparison string: `">0.95"`, `"<5000"`, `">=4.0"`, etc.
- `weight` — how much this goal matters (1.0 = baseline, 2.0 = double-weighted in achievement score).
- `window_days` — rolling window for the metric (7 for noisy signals, 30 for slow-moving ones, 60–90 for rare events like PR reverts).

**Optional category-specific fields:**
- `sections` (for `required_sections_present`) — list of section names that must appear in `output_text`.
- `min_chars` (for `min_output_chars`) — character threshold.

## Breach semantics

- A goal is **breached** on a given day if the metric value over `window_days` does not satisfy `target`.
- A goal is **persistently breached** if it has been in breach for **3 consecutive evaluation windows** (so 3 days for a 7-day window; 3 weeks for a 30-day window).
- Persistent breaches drive the self-improvement loop — they enter the improvement-analyst's backlog with priority = `weight × consecutive_breach_days`.

## Per-agent weight conventions

As a calibration guide:

- `weight: 3.0` — existential for the agent's purpose (e.g. overnight-pr's `pr_merge_rate`).
- `weight: 2.0` — mission-critical (delivery to operator, no errors).
- `weight: 1.0` — important default (error rate, recovery rate).
- `weight: 0.5` — preference, not blocking.

## How goals drive self-improvement

The `buddy` agent (docs/agents/buddy.yaml) runs this loop. Previously split between `improvement-analyst` (analysis) and manual review; unified on 2026-04-19.

1. **Review** — every hour, `buddy_review_pass` samples recent runs per agent and writes `agent_reviews` rows (`reviewer_type='buddy'`) with rating + dimension + specific_issue + suggested_action. Sonnet 4.6 phrases evidence, never invents content.
2. **Aggregate** — every 6 hours, `buddy_aggregate_findings` groups recent reviews with goal breaches from `detect_goal_breach`. Each finding carries the current metric value as a `baseline` for later verification.
3. **Queue** — one CRM task per finding, tagged `nightwatch+self-improve+<agent>+<metric>`, assigned to `auto-agent`. Dedups against open tasks for the same (agent, metric).
4. **Execute** — `auto-agent` picks up the task, reads the evidence + corrective-action template, and proposes a fix (instruction edit, manifest tuning, code change via overnight-pr, etc.).
5. **Verify** — 48h after the task moves to DONE, `buddy-grader` re-computes the metric. Pass → tag `verified_resolved`. Fail → tag `verify_failed`, re-open at `escalation:N`. At escalation:2 the task routes to `auto-researcher`; at escalation:3 it is marked `requires_human=true` and auto-escalation stops.
6. **Hold-check** — 7 days after `verified_resolved`, the grader re-checks again and tags `held_7d=true|false`. The weekly `buddy-auditor` reads the hold-rate and auto-pauses the loop if fixes aren't sticking.

## Anti-patterns to avoid

- **Goal gaming**: if an agent is hitting all goals but the operator is dissatisfied, the goals are wrong. Run the monthly goal-review (P3.6) to correct.
- **Vanity metrics**: don't use metrics that always hit target (e.g. `error_rate < 1.0` is meaningless).
- **Orphan metrics**: don't add a metric that no corrective-action template knows how to fix — the loop can't use it.
- **Window mismatch**: a 7-day window on a once-a-month event produces noise. Match window to signal frequency.

## See also

- `docs/agents/corrective-actions.yaml` — category → remediation template library.
- `robothor/engine/goals.py` — metric computation + breach detection.
- `infra/migrations/031_agent_reviews.sql` — where ratings and action items live.
- `infra/migrations/030_buddy_effectiveness.sql` — buddy's `effectiveness_score` is populated from goal achievement.
