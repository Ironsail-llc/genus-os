# Runbook — the benchmark harness must not fail agents for its own limits

**Owner:** ops · **Code:** `robothor/engine/tools/handlers/benchmark.py`
**Tests:** `robothor/engine/tests/test_benchmark_harness_fairness.py`
**Related:** [`BENCHMARK_SANDBOX.md`](BENCHMARK_SANDBOX.md) (the tool-split it
builds on), [`BENCHMARK_DECONTAMINATION.md`](BENCHMARK_DECONTAMINATION.md)

A grade is only about the agent if the harness gave the agent a fair run. Three
harness defects were producing low grades that said nothing about agent quality.
All three are fixed; this runbook records what to check when a suite's numbers
look wrong.

## 1. The sub-agent tool allow-list is DERIVED — do not hand-edit it

`_BENCHMARK_READONLY_TOOLS` is computed from `READONLY_TOOLS`
(`robothor/engine/tools/constants.py`) plus `_BENCHMARK_EXTRA_READS`, minus
`_BENCHMARK_WITHHELD_READS`. It used to be a second, hand-maintained copy of
"tools with no side effects", and it had rotted:

| Tool stripped | Whose procedure it broke |
|---|---|
| `get_knowledge_gaps`, `get_stats` | curiosity-engine — **step 1 of 7** |
| `list_agent_reviews`, `get_agent_review`, `get_fleet_achievement_score` | agent-architect, which is *instructed* to cite a `review_id` |
| `experiment_status` | agent-architect dispatch checks |
| `devops_query_metrics`, `render_devops_report` | devops-analyst |

Meanwhile `create_goal` / `update_goal` — durable **writes** — reached every
benchmark sub-agent, because `ToolRegistry._get_filtered_names` force-adds
`GOAL_TOOLS` *after* intersecting `tools_allowed`, so subtracting from the
manifest's list never touched them. Production transcripts show benchmark
sub-runs of agent-architect calling `update_goal`.
`_benchmark_tools_denied` now names `GOAL_TOOLS - allowed` explicitly.

**To classify a new tool:** add it to `READONLY_TOOLS` if it has no side
effects, or to `_BENCHMARK_EXCLUDED_TOOLS` if it must stay out.
`test_every_registered_tool_is_classified` fails the build until you do — that
parity check is the thing that stops the list rotting again.

Auditing `READONLY_TOOLS` before deriving from it turned up one genuine
misclassification: **`receive_agent_messages` is not a read.**
`Messenger.receive` is an `rpop`, so reading an agent's inbox destroys it. It
has been removed from `READONLY_TOOLS` (plan mode could have drained a live
inbox too) and named in `_BENCHMARK_EXCLUDED_TOOLS`.

## 2. The judge sees the whole answer

`_JUDGE_OUTPUT_CHARS = 12000` (was a bare `output[:3000]`). Measured across
`agent_runs`, 336 completed benchmark cases from the four weakest agents ran
past 3000 characters, averaging ~4.5K: agent-architect `fleet-analysis` (61),
`status-file-write` (47), curiosity-engine `efficiency-completion` (36),
devops-analyst `structured-report` (33), agent-architect `structural-detection`
(29). With a 4-item rubric and `PASS_THRESHOLD = 0.7`, one rubric item whose
evidence lived in the truncated tail fails the whole case.

12000 covers 98.5% of those outputs whole. Past the window `_judge_excerpt`
keeps **head + tail** with an explicit `[… N characters omitted …]` marker,
because conclusions and recommendations — what rubric items usually ask about —
live at the end.

## 3. A timeout is an outcome, not a grade

The per-task wall-clock cap was hardcoded at 240s, against a production fleet
that runs with **no** wall-clock kill (`docs/agents/_defaults.yaml` sets
`timeout_seconds: 0`).

Measured for agent-architect: production runs over 30 days mean **512.8s**, max
**728.5s**, **zero** production timeouts — while **26%** (44/170) of its
benchmark sub-runs were killed. The cap sat inside the agent's normal duration
distribution; fleet-wide, the p99 of completed benchmark sub-runs is 215.6s, so
240s clipped the tail by design.

Worse, the kill was **scored**. `AgentRunner.execute` absorbs the cancellation
and returns a `TIMEOUT` run with an empty `output_text` instead of re-raising,
so `asyncio.timeout.__aexit__` sees no exception and the harness graded the
empty string. Every `must_not_contain` pattern passes against `""`. Killed
cases were filed as ordinary partial credit — `fleet-analysis` 0.5,
`cross-pollination` 0.4, `no-direct-optimization` 0.667 — as if the agent had
half-answered.

Now:

* **`_DEFAULT_TASK_TIMEOUT_SECONDS = 900`**, overridable per suite with
  `task_timeout_seconds:` and per task with `timeout_seconds:`. A non-positive
  value is logged and ignored, never honoured as "no cap" — the fleet benchmark
  runs unattended overnight.
* A timeout records `outcome: "timeout"`, `timed_out: true`, `timeout_seconds`,
  and a hard **0.0** — not whatever the vacuous checks awarded an empty string.
* Every task result carries `outcome`: `scored` | `timeout` | `error` |
  `skipped`. Only `scored` is a statement about the agent.
* Run records and the `benchmark_run` return value carry a `timeouts` count
  beside `judge_errors`. Both are counts of cases the harness failed to grade.

Vacuous grading could also produce **false passes**, not only false failures. A
task whose `expected` holds only `must_not_contain` patterns scores **1.0** on
an empty output. `crm-enrichment`'s `no-overwrite-existing` and
`no-exec-for-file-ops` are exactly that shape, and 16 of their runs were
harness kills; on at least 8 of those days the case does not appear in
`benchmark_results.failures` at all — it was banked as a passed safety case for
a run that produced nothing. A timeout is now never scored, so that class is
gone.

**Reading a suite's numbers:** if `timeouts` is climbing, raise that suite's
`task_timeout_seconds` — do not optimise the agent. If `judge_errors` is
climbing, the grader is broken, not the agent.

## Suite YAML keys added

```yaml
id: agent-architect
agent_id: agent-architect
task_timeout_seconds: 1200      # optional; per-suite wall-clock cap
tasks:
  - id: fleet-analysis
    timeout_seconds: 1800       # optional; overrides the suite value
```
