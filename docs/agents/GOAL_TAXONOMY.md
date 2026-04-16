# Agent Goal Taxonomy

Every agent in Robothor has an explicit **goals** contract in its YAML manifest. Goals are the primary signal the self-improvement loop uses to decide what to fix. When a goal breaches persistently, the improvement-analyst selects a corrective-action template (see `brain/policies/corrective-actions.yaml`) and queues a Nightwatch task to fix the root cause — prompt, flow, tools, model, config, whatever is needed to restore the goal.

This file is the shared reference. Every manifest must follow it.

## The four categories

Goals fall into exactly one of these categories. Each category maps to a remediation class — the kind of change the self-improvement loop proposes when the goal breaches.

| Category        | What it measures                                             | When breached, the loop typically changes…                                       |
| --------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------- |
| **reach**       | Did the output get to the intended recipient?                 | Delivery channel config, bot token, routing rules, fallback channel.              |
| **quality**     | Is the output substantive and accurate?                       | Instruction prompt, warmup context, tool selection, model tier, required sections. |
| **efficiency**  | Was it produced within budget (cost, time, iterations)?       | `max_iterations`, `stall_timeout_seconds`, model (Opus→Sonnet→MiMo), prompt size. |
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
| `avg_duration_ms`                     | efficiency   | mean `duration_ms` over window                             |
| `p95_duration_ms`                     | efficiency   | 95th percentile `duration_ms`                              |
| `avg_cost_usd`                        | efficiency   | mean `total_cost_usd`                                      |
| `p95_cost_usd`                        | efficiency   | 95th percentile cost                                       |
| `timeout_rate`                        | efficiency   | `status='timeout'` / total runs                            |
| `error_rate`                          | correctness  | `status='failed'` / total runs                             |
| `tool_success_rate`                   | correctness  | fraction of tool calls with no error                       |
| `task_completion_rate`                | correctness  | resolved tasks / created tasks                             |
| `experiment_measure_success_rate`     | correctness  | experiment_measure calls without error (auto-researcher)   |
| `pr_merge_rate`                       | quality      | merged PRs / created PRs (Nightwatch)                      |
| `pr_revert_rate`                      | correctness  | reverted-after-merge PRs / merged PRs                      |

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
- `weight: 1.0` — important default (normal timeouts, cost).
- `weight: 0.5` — preference, not blocking (nice-to-have speed targets).

## How goals drive self-improvement

1. **Compute** — nightly, `robothor.engine.goals.compute_goal_metrics(agent_id)` runs for every active agent.
2. **Detect** — `detect_goal_breach(agent_id)` flags persistent breaches.
3. **Classify** — each breach is categorized (reach/quality/efficiency/correctness). The category maps to a remediation template.
4. **Queue** — the improvement-analyst selects the highest-priority breach and queues a Nightwatch task with the template's investigation + fix scope.
5. **Execute** — overnight-pr receives the focused brief and opens a PR.
6. **Review** — the operator (or automated tests + CI) approves or rejects.
7. **Close the loop** — after 2 windows, `compute_goal_metrics` re-runs. Goal recovered → record success in `agent_reviews`. Still breached → escalate to the next template in the list.

## Anti-patterns to avoid

- **Goal gaming**: if an agent is hitting all goals but the operator is dissatisfied, the goals are wrong. Run the monthly goal-review (P3.6) to correct.
- **Vanity metrics**: don't use metrics that always hit target (e.g. `error_rate < 1.0` is meaningless).
- **Orphan metrics**: don't add a metric that no corrective-action template knows how to fix — the loop can't use it.
- **Window mismatch**: a 7-day window on a once-a-month event produces noise. Match window to signal frequency.

## See also

- `brain/policies/corrective-actions.yaml` — category → remediation template library.
- `robothor/engine/goals.py` — metric computation + breach detection.
- `infra/migrations/031_agent_reviews.sql` — where ratings and action items live.
- `infra/migrations/030_buddy_effectiveness.sql` — buddy's `effectiveness_score` is populated from goal achievement.
