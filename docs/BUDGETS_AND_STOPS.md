# Budgets and Stops

Every place an agent run (interactive chat, scheduled, workflow step, hook,
sub-agent) can be **refused**, **stopped** partway or **degraded** by a budget,
a cap or a stop, with the setting, its default, and what you see.

**Budget** means a limit on spend: dollars, tokens or calls. **Control** means
something else: a stop you issued, a timeout, a safety valve. The platform's
direction is that budgets report and controls act. A spend budget whose limit
you never set observes and alerts. It does not stop the run.

## How a stop is recorded

A stop marked **yes** in the `budget_stop` column below leaves one structured
run step of type `budget_stop`:

| Field (step `tool_input`) | Meaning |
|---|---|
| `stop` | Which budget or control. Also the step's `tool_name`. |
| `reason` | The sentence the engine logged |
| `setting` | The environment variable or manifest field that controls it |
| `limit` | The limit in force |
| `observed` | The value that crossed it |
| `mode` | `observe`: recorded, and the run went on. `enforce`: the run was stopped or refused. |

The same fields, plus `run_id`, `agent_id` and `tenant_id`, are published as an
`agent.budget_stop` event on the `robothor:events:telemetry` Redis stream. The
publish happens off the event loop. The Runs view shows the step in the run's
step list, and
`genus doctor --only budget_stops.recent` counts the last 24 hours by stop and
mode:

```bash
genus doctor --only budget_stops.recent
```

An observed hourly cap crossing also raises a `warning` alert through the
existing alert path, which writes a digest row rather than paging. The
runaway-token cap keeps its `critical` alert in both modes.

## Spend budgets that observe unless you set them

| Budget | Setting (default) | Unset (default) | Set explicitly, or `*_MODE=enforce` |
|---|---|---|---|
| Fleet hourly cost cap (`hourly_cost_cap`) | `ROBOTHOR_HOURLY_COST_CAP_USD` (5.0); `ROBOTHOR_HOURLY_COST_CAP_MODE` (empty) | Scheduled runs are admitted past the cap. The run carries a `budget_stop` step with mode `observe`, and a warning alert is raised. | Scheduled runs are deferred (a SKIPPED run row) once the rolling hour reaches the cap. The SKIPPED row carries the step with mode `enforce`. |
| Runaway-token hard cap (`runaway_token_cap`) | `ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS` (5,000,000); `ROBOTHOR_RUNAWAY_HARD_CAP_MODE` (empty) | The step (mode `observe`) and the critical alert are recorded once. The run continues. | The run ends with `budget_exhausted`, and the step has mode `enforce`. |

Both modes and the hourly cap are ordinary settings: set them in the
environment, in config.yaml, or with `genus config set`, and the engine reads
them at start. The runaway cap's token count comes from the environment
variable only; setting it in config.yaml alone switches the guard to enforce at
the default. When the runaway cap observes, its pages say the run was **not**
contained.

The hourly cap is only consulted when fleet admission is on
(`ROBOTHOR_ADMISSION_ENABLED` with `ROBOTHOR_ADMISSION_MODE`). It never applies
to interactive runs. `ROBOTHOR_HOURLY_COST_CAP_MODE=observe` wins over an
explicit cap, for a deployment that sets the cap and wants it to report only.
Concurrency admission, meaning the slot cap and the reservation for
interactive work, is capacity rather than spend, and is unchanged. See
[Configuration](reference/configuration.md) for every setting.

## Inventory

**Mid-run?** answers whether the *default* can stop or refuse an interactive
or workflow run partway through.

### Admission and scheduling

| Stop | Where | Setting (default) | Mid-run? | What you see | Kind | `budget_stop` |
|---|---|---|---|---|---|---|
| Fleet hourly cost cap | `pool.py` `FleetPool.can_start`, `admission.py` `admit` | See above | No (observes by default) | Observe: the step on the run. Enforce: a SKIPPED run "Deferred by admission control: Hourly cost cap reached", plus a guardrail event. | Budget | yes |
| Fleet concurrency and interactive reservation | `pool.py` `can_start` | `ROBOTHOR_MAX_CONCURRENT_AGENTS` (3), `ROBOTHOR_RESERVED_INTERACTIVE_SLOTS` (1), admission flags (off) | Refuses scheduled fires only | SKIPPED deferral row and a guardrail event | Control (capacity) | no |
| Capacity governor | `capacity_governor.py` | Mode policy and thermal readings | No; lowers the pool's slot count | Log "Capacity: mode=..." | Control | no |
| Cron dedup, circuit breaker, safety timeout | `scheduler.py` `_run_scheduled` | 5 consecutive errors (constant); timeout + 120 s | Refuses scheduled fires | Log "Cron skipped"; the breaker opens a CRM task "paused" | Control | no |
| Workflow already running or rate-limited | `workflow.py` | `rate_limit_per_minute` (0, meaning 30) | Refuses at start | SKIPPED "Skipped: workflow already running" or "Deferred: tool-call rate limit reached" | Control / budget (calls) | no |

### The run loop

| Stop | Where | Setting (default) | Mid-run? | What you see | Kind | `budget_stop` |
|---|---|---|---|---|---|---|
| Runaway-token hard cap | `loop_guards.py` `_runaway` | See above | No (observes by default) | Observe: the step and a critical alert. Enforce: `runaway_token_cap_hit`, `budget_exhausted`. | Budget | yes |
| Runaway-token soft alert | `loop_guards.py` `_runaway` | `ROBOTHOR_RUNAWAY_ALERT_TOKENS` (500,000) | No; alert only | Alert "Runaway-token alert" | Budget (alert) | no |
| Per-agent cost cap | `runner.py` `_run_loop` hard-budget check | `v2.hard_budget` (false) and `v2.max_cost_usd` (0) | No; off unless the manifest sets both | A forced final summary, "Hard budget limit reached (explicit cost cap).", `budget_exhausted` | Budget (explicit) | yes, `hard_budget`, enforce |
| Per-agent cost pre-flight | `runner.py` `_run_loop` | Same as the per-agent cost cap | No; off by default | Tools removed for the next call | Budget (explicit) | no |
| Goal token budget | `goals/runtime.py`, `runtime/provider_budget.py` | The goal's explicit token budget (none) | Only for goal runs that set one | "Explicit goal token budget exhausted" | Budget (explicit) | no |
| Funded request budget | `request_budget.py`, `brave_budget.py` | Opt-in budget scopes only (sales, benchmarks) | No by default | `RequestBudgetError` text | Budget (explicit) | no |
| Iteration safety cap | `runner.py` `_run_loop` | Manifest `safety_cap` (200; heartbeat 50, worker 80; 0 = none) | Yes, at 200 iterations | A forced final summary, "Safety limit reached (N iterations).", `budget_exhausted` | Control (safety) | yes, `safety_cap`, enforce |
| Soft check-in | `run_pacing.py` | `ROBOTHOR_MAX_ITERATIONS` (20) | No; a nudge | An engine note | Control | no |
| Error escalation abort | `runner.py` | 10 total errors (constant) | Yes | "Too many errors (N total)." | Control (safety) | no |
| Context compaction and ceiling | `context_budget.py`, `context_fit.py` | `ROBOTHOR_COMPACTION_TRIGGER_TOKENS` (80,000) | Degrades only (history summarised) | An engine note; log "Proactive compaction" | Budget (tokens), degrade | no |
| Wall-clock run budget, wrap-up and expiry | `run_deadline.py` | `ROBOTHOR_RUN_WRAPUP_FRACTION` (0.9), `ROBOTHOR_RUN_BUDGET_SECONDS` (0), only under `ROBOTHOR_STEP_EFFICIENCY_MODE=enforce` | No by default | "Wall-clock budget exhausted", plus a guardrail event `run_budget` | Control (time) | no; guardrail event |
| Loop wall-clock self-check, stall watchdog, outer timeout | `loop_guards.py`, `stall_watchdog.py`, `runner.py` | Agent `timeout_seconds` (600; Telegram 86,400); `ROBOTHOR_MAX_WALLCLOCK_SECONDS` | Yes, at the timeout | TIMEOUT, "Circuit-breaker hard timeout" or "No progress for Ns" | Control (time) | no |
| Durable operator stop (pause or cancel) | `runtime/controls.py`, `runtime/provider_budget.py`, `runtime/failure.py` | `agent_runtime_controls` rows you write | Yes, on your instruction | CANCELLED "Stopped as requested." | Control | yes, `durable_stop`, enforce |
| Operator interrupt | `loop_guards.py` | Your action | Yes | CANCELLED | Control | no |
| Host runtime deadline | `runtime/deadlines.py` | The managed host's deadline | When the host sets one | "Runtime deadline expired" | Control | no |
| Finalization budget | `finalization_budget.py` | 60 s per step, 150 s total (constants) | After the loop; skips steps | Log line | Control | no |
| Daemon shutdown budget | `shutdown_budget.py` | 12 s plus 3 s margin (constants) | On restart | "Cancelled: engine shutdown mid-run" | Control | no |

### Models, tools and sub-agents

| Stop | Where | Setting (default) | Mid-run? | What you see | Kind | `budget_stop` |
|---|---|---|---|---|---|---|
| Per-call LLM timeout | `llm_budgets.py`, `llm_client.py` | `ROBOTHOR_LLM_TIMEOUT` (120), `ROBOTHOR_LLM_TIMEOUT_BATCH` (300) | Degrades to the next model; the whole chain failing fails the run | Retry log lines; FAILED | Control (time) | no |
| Model breaker and spent credentials | `llm_client.py` | `ROBOTHOR_MODEL_BREAKER_THRESHOLD` (3), `ROBOTHOR_MODEL_BREAKER_COOLDOWN` (600) | Degrades (fallback model) | Log line and breaker alert | Control | no |
| Workflow deadline and outer timeout | `workflow_budget.py`, `workflow.py` | Workflow `timeout_seconds` (900) | Yes | Step or run TIMEOUT naming the step | Control (time) | no |
| Tool rate limit | `guardrails.py` | `v2.rate_limit_per_minute` (0, meaning 30/min) | Refuses individual tool calls; the run continues | "Rate limit exceeded: N/30 calls/min" | Budget (calls), degrade | no; guardrail event |
| Per-tool timeout | `tool_turn.py`, `tools/registry.py` | `v2.tool_timeout_seconds` (120) | The tool fails; the run continues | "Tool 'x' timed out" | Control | no |
| `execute_code` and RLM caps | `tool_turn.py`, `rlm_tool.py` | `ROBOTHOR_EXECUTE_CODE_MAX_CALLS` (200), `ROBOTHOR_RLM_MAX_BUDGET` (2.0) | Refuses the tool call | "RLM budget exceeded" | Budget / control | no |
| Spawn limits | `tools/handlers/spawn.py`, `spawn_limits.py` | Nesting depth 2, `ROBOTHOR_MAX_SPAWN_BATCH` (10), `ROBOTHOR_MAX_CONCURRENT_SPAWNS` (10) | Refuses the spawn call | "Max nesting depth exceeded" and similar | Control | no |
| Memory block budget | `memory/blocks.py` | `MEMORY_BLOCK_BUDGET` (off) | No | A note naming the block | Budget | no |

`robothor/autonomy/budget.py` is a monthly spend projection, not a run stop.

## Open questions for the operator

- **No default spend stop.** With neither cap set, no spend budget stops any
  run by default. A runaway interactive run is bounded only by its timeout
  (86,400 s on Telegram). This is the intended change and needs your explicit
  sign-off. To restore the old protection, set `ROBOTHOR_RUNAWAY_HARD_CAP_MODE=enforce`.

- **Iteration safety cap.** `safety_cap` defaults to 200 and stops a run
  partway. It is recorded as a `budget_stop` step and still enforces. It is
  infinite-loop protection rather than spend, so making it observe by default
  is your decision.
- **Tool rate limit.** The default of 30 calls per minute refuses individual
  tool calls. The run continues, and a guardrail event is written.
- **Stops without a `budget_stop` step yet.** The wall-clock, stall, LLM-timeout
  and workflow-deadline stops are controls. They already end the run with a
  TIMEOUT or FAILED status and an ERROR step. They do not record a
  `budget_stop` step yet.
