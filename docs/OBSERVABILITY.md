# Observability — OpenTelemetry traces

The engine emits OpenTelemetry-compatible traces per agent run: a trace per run,
nested `tool_call` and `llm_call` spans, with **full span attributes and
durations** and OpenTelemetry **GenAI semantic conventions** on LLM spans.

## Enable export

Set an OTLP/HTTP endpoint; the engine POSTs traces to `<endpoint>/v1/traces`
(no extra dependency — it uses the built-in httpx exporter):

```bash
systemctl set-environment OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
systemctl restart robothor-engine
```

Works with any OTLP/HTTP intake:
- **Jaeger** all-in-one: `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318`
- **Grafana Tempo**: point at the Tempo OTLP/HTTP receiver (`:4318`)
- **Datadog**: run the Datadog Agent with OTLP/HTTP enabled and point here

## What's emitted

Each run's trace has one root span, `agent_run`, that opens when the run starts
and closes when it exits, so its duration is the run's turn time. It carries
`run.status` and `run.trigger_type`. Every `llm_call`, `tool_call` and
`run_summary` span of the run is its child, and a sub-agent's `agent_run` is a
child of the parent run's span. Spans carry an OpenTelemetry span kind:
`agent_run` is SERVER, `llm_call` is CLIENT, everything else is INTERNAL. In a
backend that names trace metrics by span kind, such as Datadog, the operations
are `server.request` (`agent_run`), `client.request` (`llm_call`) and
`Internal` (`tool_call`, `run_summary`), with the span name as the resource.

Per `llm_call` span (GenAI conventions):
- `gen_ai.system` (anthropic / openai / google / …)
- `gen_ai.request.model`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons`

Resource attributes: `service.name=robothor-engine`, `agent.id`, merged with the
standard OpenTelemetry environment: `OTEL_RESOURCE_ATTRIBUTES` (comma-separated
`key=value`, values percent-decoded, e.g.
`deployment.environment=staging,service.namespace=example`) overrides those
defaults, and `OTEL_SERVICE_NAME` overrides `service.name` from either. Span status
uses OTLP codes (1 = ok, 2 = error). Run metrics are also published to the Redis
stream `robothor:events:telemetry` for the dashboards.

## Publishing never blocks a run

Every run publishes its trace when it ends, whatever the outcome: completed,
failed, timed out, aborted or interrupted. The `agent_run` and `run_summary` spans carry
`run.status`; a failed, timed-out or cancelled run exports it with error status
(OTLP code 2), the reason as the status message and as `error.message`, and the
Redis entry's `status` is the run's terminal status.

Run metrics go to the Redis the rest of the engine uses (`ROBOTHOR_REDIS_HOST`,
`_PORT`, `_DB`, `_PASSWORD`); the older `REDIS_HOST` / `REDIS_PORT` still apply
when `ROBOTHOR_REDIS_HOST` is left at its default. Each run is published once.

Publishing happens off the engine's event loop: the
Redis entry and the OTLP export run as background tasks on one shared async Redis
client and one shared HTTP client (5 s timeout). Traces that finish while an export
is in flight are sent together in the next request. A sink that is down logs one
warning, then stays quiet at debug level; it never fails or delays the run.

## Failure-mode detectors

`robothor/engine/detectors.py` holds read-only observers the daemon watchdog
runs on a schedule. They never kill a run — each queries one signal, compares
it against a threshold, and calls `alerts.alert()`. `warning` lands in the
`crm_agent_notifications` digest; `critical` pages Telegram. Repeat alerts are
deduped in-process (1h for fast signals, 24h for the sustained-outage ones).

| Detector | Cadence | Fires when |
|----------|---------|-----------|
| `repeat_error_detector` | 5 min | same (agent, error_type) ≥3× in an hour |
| `tool_degradation_detector` | 5 min | a tool spikes: ≥5 failures, or >50% of ≥10 calls, in an hour |
| `tool_outage_detector` | 2 h | a tool is ~totally dead: ≥8 calls and ≥95% failures over 7 days |
| `primary_model_unreached_detector` | 2 h | ≥50% of an agent's runs (≥10 in 7 days) never reached its configured primary model |
| `search_degradation_detector` | 2 h | ≥50% of ≥3 `web_search` calls in 6 h came from a fallback or failed |
| `search_quota_detector` | 2 h | Brave reports under a tenth of the month left (warning) or none (critical) |
| `runaway_burn_detector` | 2 min | a running run has crossed 500K tokens |
| `zombie_runner_detector` | 10 min | `running` >15 min with no step activity for 5 min |
| `stuck_workflow_detector` | 10 min | `workflow_runs` running past timeout + grace |
| `workflow_failure_streak_detector` | 10 min | the same workflow's last 3 terminal runs all failed |

Disable all of them with `ROBOTHOR_DETECTORS_ENABLED=0`.

`tool_degradation_detector`, `tool_outage_detector`, and `tracking.get_tool_stats`
all exclude rows the benchmark sandbox refused (`error_type='sandbox_denied'`,
set when the tool result carries the structural marker `guard: "is_benchmark"`
or, as a fallback, when the message starts with `benchmark sandbox:`) from
their call and failure counts — those are the nightly benchmark runner being
correctly denied write access, not evidence a tool is broken.

### Why two tool detectors

`tool_degradation_detector` looks at one hour and needs ~5 failures in it, so it
only sees *acute* breakage on a busy tool. A tool called twice a day can be 100%
dead forever without ever putting 5 failures in the same hour — which is how a
low-traffic external-search tool failed 32/32 (`error_type=auth`) across 14
days with nothing alerting. `tool_outage_detector` trades resolution for reach: a 7-day
window, a volume floor (≥8 calls) so it cannot fire on noise, and a failure
ratio (≥95%) so firing means "this dependency is gone", not "this is flaky". An
outage still dead ≥3 days escalates from `warning` to a `critical` page.

The window has to be that long. Over 24 hours the same live outage produced 3
calls — below any honest volume floor — so a 24-hour window would have watched
a totally dead dependency and stayed silent.

### Declaring a known outage

Some outages are decisions, not incidents. Declare them so the detector stops
alerting and records why:

```bash
systemctl set-environment \
  ROBOTHOR_DECLARED_TOOL_OUTAGES="sms_send:carrier contract ended 2026-08-01"
```

Format is `tool:reason,tool:reason`; the reason is logged every time a
suppression takes effect. The vision tools (`look`, `who_is_here`, …) are
additionally suppressed while the vision service reports itself
administratively disabled — that one expires on its own when the service is
re-armed. There is no other exemption: anything undeclared alerts.

### Primary-model loss

`llm_client` logs `PRIMARY model failed, falling back` at warning level and the
run then completes normally, so the fleet can spend days on its fallback chain
with every run green. `agent_runs.models_attempted` records which models
actually served the run; `primary_model_unreached_detector` compares that
against each manifest's `model.primary`.

Ids are compared through `model_registry.canonical_model_id()`: manifests
configure `openrouter/xiaomi/mimo-v2.5` while the provider reports back
`xiaomi/mimo-v2.5` (sometimes with a dated release slug). Comparing the raw
strings marks *every* healthy run a fallback. A run that started on a fallback
and later reached the primary counts as reached — the alert is about a primary
that cannot be reached at all, not about one retry.

## Run truth in the Helm — ran, delivered, completed

The Helm's **Automations** view (`?v=workflows`) shows one card per scheduled
agent and answers three independent questions about its last run, because
collapsing them into one status pill is how a nightly briefing goes days
without arriving behind a green badge:

- **Ran** — `agent_runs.started_at` and `status`. "Never run" is its own answer
  and the most actionable thing the view can say about a schedule just written.
- **Delivered** — `delivery_status`, `delivered_at` and `delivery_channel`. A
  run can finish and still fail to send. An agent whose `delivery.mode` is
  `none` reads **none expected**, never as a failure: most worker agents are
  deliberately silent and communicate through CRM tasks.
- **Completed** — `verified_status` (the run verifier's verdict) falling back to
  `outcome_assessment` (the agent's own rating). With neither, the cell reads
  "not assessed" rather than borrowing the run status — a run that exited zero
  is not a run that did the job.

`GET /api/automations` composes each card from three sources: the manifest (what
the automation is), `agent_schedules` (what the scheduler currently holds —
`next_run_at`, `consecutive_errors`) and the newest `agent_runs` row (what
happened). Both queries are scoped to the platform tenant *in the statement*,
not only by RLS. The same four run columns now come back from `GET /api/runs`
and `GET /api/runs/{id}`, so the Runs view shows them too.

**One card per job, not per agent.** The scheduler derives up to three jobs from
one manifest and keys `agent_schedules` — and the circuit breaker — by the job
id it built: `<agent>` from `schedule.cron`, `<agent>:heartbeat` from
`heartbeat.cron`, `<agent>:worker` from `worker.cron`
(`robothor/engine/schedule_reconcile.py`). Automations uses the same ids, so an
agent scheduled only by a heartbeat and a worker — the shape of the primary
agent on a typical instance — gets a card per job rather than none at all. The
card's **Enabled** toggle and **Run now** address the *manifest* (one
`schedule.enabled` covers all three jobs); **Reset breaker** addresses the *job*,
because that is what trips. **Edit schedule** appears only on an `agent` job:
the manifest PATCH owns `schedule.cron` and `schedule.timezone` and nothing
else, so heartbeat and worker crons are edited in the YAML.

Where the manifest and the schedule row disagree, the manifest wins for
anything the operator can change (cron, timezone, enabled) — it is what the
engine re-derives from on its next reconcile. Two honesty rules follow from
that: a card whose manifest will not parse is still rendered, from the schedule
row alone and marked as such (the engine keeps firing a job it already holds, so
a YAML typo must not make a running automation disappear); and a disabled or
breaker-tripped card prints "not scheduled — disabled" / "held — reset the
breaker to resume" rather than a cron-arithmetic instant it will not fire on.
Likewise, a write is reported as reconciled only when the response's
`reconcile.applied` says so — an engine that is down answers 200 and changes
nothing.

**The circuit breaker.** The scheduler stops running an agent after
`CIRCUIT_BREAKER_THRESHOLD` consecutive failures (`robothor/engine/scheduler.py`
— the Helm imports that constant rather than copying it). A tripped automation
carries a chip naming the count and the threshold, and **Reset breaker** — an
audited `POST /api/automations/{id}/reset-breaker`, operator-only and
tenant-scoped — zeroes `consecutive_errors` so it fires again on its next
scheduled run. Before this the only way back was an `UPDATE` typed into psql.

## Workflow runs, step by step

A workflow run records what each tool step was given and what it returned: the
tool name and its rendered arguments are written to `workflow_run_steps` when
the step starts (so a step that never returns still shows what it was asked to
do), and the tool's output when it ends. A credential-named argument, or the
value of a `vault_set` step, is stored as `<redacted>`, and credential-shaped
values in the output (API keys, tokens) are replaced with a marker naming
their kind, as the agent path does for a tool result. Each is capped at 64 KB
of JSON; a larger payload is stored as `{"_truncated": true, "original_bytes":
N, "preview": "..."}` and the run carries on. NUL characters are dropped and
`NaN`/`Infinity` are stored as text, since JSONB rejects both.

`GET /api/workflows/{workflow_id}/runs` on the bridge lists a workflow's recent
runs with `steps_completed`, `steps_failed` and `steps_skipped`, and
`GET /api/workflows/{workflow_id}/runs/{run_id}` returns one run with each
step's `tool_name`, `tool_input`, `tool_output` and `error_message`. The step
payloads hold whatever the tools saw, so the route is for owners and admins
only (other roles get 403) and is sent with `Cache-Control: no-store`.

In the dashboard, Automations → Workflows reads the same two routes: a workflow
that has run expands to list its runs, and opening one shows each step's tool,
status, error, and input and output as collapsible JSON. A truncated payload is
shown as its preview with the original size. A scheduled plugin job records no
steps, so its run shows the value the job returned instead (the detail route's
`result`, with `result_truncated` when it was cut).

## Runs in the Helm — every run, and what one run did

The Helm's **Runs** view (Observe › Runs, `?v=runs`) is operator-only and
read-only. It has four parts:

- **A summary strip** over the chosen period: runs, failed (failed or timed
  out), delivered, and cost. It comes from `GET /api/runs/summary`, which counts
  by status, by delivery category and by agent. Benchmark runs are left out
  when decontamination is enforced, with the same predicate the analytics module uses
  (`robothor/engine/analytics.py`, `exclude_benchmark_filter`).
  The summary leaves benchmark runs out only when decontamination is enforced,
  and the table always shows them, so the two can differ by exactly those rows.
- **Filters** for status, agent, trigger and period (24 hours, 7 days — the
  default — 30 days, or all time). They change both the table and the summary.
- **A full-width table** with agent, trigger, status, started, duration, model,
  tokens, cost, delivery and verification, 50 rows at a time with **Load more**
  (`GET /api/runs?status=&agent=&trigger=&since=<hours>&limit=<≤200>&offset=`).
- **A run detail drawer**, opened by selecting a row (`GET /api/runs/{id}`, sent
  with `Cache-Control: no-store`). Its header carries the trigger and its
  detail, start, duration, the models tried, tokens including cache reads and
  writes, cost, the delivery reading, the verifier's verdict, the agent's own
  outcome assessment with its notes, and the task text. Below it, each step is a
  row (number, kind, tool or model, duration, tokens, ok or error, the error
  text); a tool step expands to its input, redacted when it was recorded, and
  its output, which the engine caps at 4,000 characters when it records it — the
  drawer says so when an output was cut. Warm-up steps are grouped under
  **Warm-up**, and guardrail events are listed with their reason.

**Delivery categories.** Delivery is read the same way by the summary
(`crm/bridge/routers/runs.py`) and the table (`app/src/lib/automations/run-truth.ts`):

| Category | `delivery_status` | Reads as |
|---|---|---|
| delivered | `delivered`, `sent`, `ok`, `success` | the channel and when it landed |
| none expected | `silent`, `suppressed_sub_agent`, `suppressed_trivial`, `suppressed_filtered`, `no_output`, `published`, `blocked_by_hook:*` | "no delivery expected", in a neutral tone |
| failed | `failed*`, `partial:<sent>/<expected>`, `unknown_mode:*` | red — the only red delivery |
| pending | none yet, run still pending, running or `awaiting_approval` | "delivery pending" |
| not recorded | none on a finished run, or a status the Helm does not know | neutral, or a warning for an unknown status |

**Steps are pruned after 30 days** (`robothor/engine/retention.py`). A run older
than 30 days keeps its row, totals and cost, and the drawer says its steps were
pruned. A more recent finished run with no steps says none were recorded.
The drawer shows what the engine stores: a step's start time is the time it was
recorded, there is no per-step cost, and an LLM step carries its model, tokens
and timing but not its prompt or reply.

A viewer is refused with 403 on every route and the drawer says operator access
is required; a run that no longer exists answers 404 and the drawer says it is
gone.

## Notes

- Export is best-effort (a 5s-timeout POST per run); a collector outage never
  blocks a run.
- The payload builder is `TraceContext.build_otlp_payload()`; GenAI attributes
  are built by `telemetry.gen_ai_attributes()` and attached at the runner's
  `llm_call` span.
