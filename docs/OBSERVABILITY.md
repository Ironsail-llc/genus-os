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

## Structured log lines

Five events are written as one `key=value` line each, so a log pipeline can
count and group them without parsing prose. Every line starts with
`event=<name>`; a value with spaces is double-quoted; an empty value is `-`.
None carries prompt text, tool arguments, result bodies or personal data, and
every `reason` (and an alert's `subject`) is cut to a bounded length, then has
credentials, email addresses and phone numbers replaced (`<redacted>`, `<email>`,
`<phone>`), then is cut to 200 characters. `genus engine failures` scrubs its
error text the same way when it prints it; the stored rows are unchanged.

| Event | Logger | Level | Fields |
|---|---|---|---|
| `tool_call` | `robothor.engine.tool_calls` | INFO when `outcome=ok`, WARNING otherwise | `tool`, `agent` (or `workflow` for a workflow step), `run_id`, `outcome` (`ok`, `error`, `refused`, `denied`, `timeout`, `cancelled`), `error_type`, `duration_ms`, `reason` |
| `llm_call` | `robothor.engine.llm_calls` | INFO on `outcome=success`, WARNING otherwise | `agent`, `run_id`, `model`, `outcome`, `error_type` (on a failure), `input_tokens`, `output_tokens`, `cached_tokens`, `finish_reason`, `latency_ms`, `streamed`, `attempt` (1-based, within one dispatch), `fallback` (0 for the first model in the chain), and `estimated=true` when the provider reported no usage and the input count is the engine's estimate |
| `run_end` | `robothor.engine.runs` | INFO when `outcome=completed`, WARNING otherwise | `agent`, `run_id`, `trigger`, `outcome` (`completed`, `error`, `cap`, `timeout`, `cancelled`), `duration_ms`, `iterations`, `input_tokens`, `output_tokens`, `total_tokens` |
| `workflow_step_failed` | `robothor.engine.workflow` | WARNING | `workflow`, `run_id`, `step`, `step_type`, `tool`, `duration_ms`, `reason` |
| `alert` | `robothor.engine.alerts` | ERROR (INFO for benchmark traffic) | `kind`, `severity`, `subject`, `alert_id` — see `docs/runbooks/PAGING.md` (repository only) |

```text
event=tool_call tool=example_tool agent=example-agent run_id=9b1c… outcome=denied error_type=role.rbac duration_ms=4 reason="Your role here is 'viewer', which may not use 'example_tool'"
event=llm_call agent=example-agent run_id=9b1c… model=openai/gpt-6-luna outcome=error_403 error_type=PermissionDeniedError latency_ms=231 streamed=false attempt=1 fallback=0
event=run_end agent=example-agent run_id=9b1c… trigger=telegram outcome=completed duration_ms=4321 iterations=3 input_tokens=9000 output_tokens=400 total_tokens=9400
```

`denied` is a role or allowlist gate; `refused` is any other engine gate, or a
redirection such as the repeat guard. A tool that failed, or another system
that refused the call, is `error`; so is a result that carries an HTTP status of
400 or above (`error_type=http_<code>`, the body never logged). An identical repeat of a call already refused or
denied in the same run is not made again and logs `outcome=refused
error_type=repeat_refused reason="repeat of a refused call"`. A `tool_call` wrapper is logged once, under
the tool it wraps. A call refused at admission, before it reaches the registry,
is logged too, from a model turn or from an `execute_code` snippet (whose own
call and approval caps log as `refused`). A workflow tool step's line names the
workflow run in `run_id`.

### Failures over a window

```bash
genus engine failures                 # the last 24 hours, this tenant
genus engine failures --hours 6 --json
```

Groups failed tool calls (`agent_tool_events`), denied calls (`audit_log`) and
failed workflow steps (`workflow_run_steps`) by source, tool, agent or workflow
and normalized error (ids and numbers collapsed, redacted), largest first, with
each tool's call count over the same window. A denial recorded in both
`audit_log` and `agent_tool_events` is counted once, as `denied`.

A streamed model call asks the provider for its usage
(`stream_options.include_usage`), so it records the same input, output and
cached tokens as an unstreamed one.

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

Disable all of them with `ROBOTHOR_DETECTORS_ENABLED=0`. The Datadog watch
runs on the 5-minute tick too, in its own module: see
[Genus watches Datadog](#genus-watches-datadog).

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
one manifest and keys `agent_schedules` — and the circuit breaker — by tenant
and the job id it built: `<agent>` from `schedule.cron`, `<agent>:heartbeat` from
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
  A spend budget the run reached (mode `observe`: the run continued), or a
  control that refused or ended it (mode `enforce`), is a `budget_stop` step,
  whose tool is the stop's name and whose input carries the reason, setting,
  limit, observed value and mode. The same
  record is published as `agent.budget_stop` on `robothor:events:telemetry`
  ([Budgets and Stops](BUDGETS_AND_STOPS.md)).

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

## Execution lanes

The engine reports how full each execution lane is
([Configuration](configuration.md)). `GET /health` carries an
`execution_lanes` block with, for `interactive` and `background`, `active`,
`waiting` and `limit`. `/metrics` has the gauges
`robothor_lane_active{lane}` and `robothor_lane_waiting{lane}`. During a burst
of scheduled runs, background `waiting` rising while interactive `waiting`
stays at 0 is the lanes doing their job; background `waiting` that never
returns to 0 means its limit is too low for the schedule.

## Dashboard web vitals

The dashboard reports its own Core Web Vitals (LCP, INP, CLS, FCP, TTFB) from
every page load. The browser beacons each metric to `POST /api/vitals`, which
writes it twice:

- **One log line per metric** on the dashboard's standard output, so any log
  pipeline that collects container output picks it up:

  ```json
  {"event":"web_vital","name":"LCP","value":812.4,"rating":"good","path":"/","view":"chat","id":"v1-…","navigation_type":"navigate"}
  ```

- **One entry on the Redis stream `robothor:events:vitals`**, capped at about
  10,000 entries. It is not one of the live event-bus streams, so the
  dashboard's own views never read it.

`rating` is web-vitals' own `good` / `needs-improvement` / `poor`. `path` is the
pathname without its query string and `view` is the dashboard view (`?v=`);
no tenant, user or session is recorded. A Redis outage loses the stream entry,
never the log line, and never fails the page.

The route is reachable signed out, so the sign-in page's vitals are kept too.
That is why it is bounded: the strict schema above, bodies up to 2 KB (413
otherwise), and 120 beacons per client per minute (429 with `Retry-After`; a
page load sends about five). The client is the address
`GENUS_DASHBOARD_TRUSTED_PROXIES` lets the dashboard verify; without that
setting every client shares one window.

Reporting is always on: every page mounts the reporter, and there is no
setting that turns it off.

Two CI gates sit beside it, both in `app/`:

- `pnpm bundle:check`, after `pnpm build`, compares each page route's gzipped
  first-load JavaScript with `app/bundle-budget.json` and fails a route over
  its budget or without one. `node scripts/bundle-budget.mjs --write` resets
  every budget to the current size plus 5%; raise one on purpose, in the pull
  request that needs it.
- `e2e/perf.spec.ts` fails when the chat takes longer than its budget to paint
  or to show the first streamed token, or when a tab holds more than one
  event-stream connection. `PERF_CHAT_PAINT_BUDGET_MS` and
  `PERF_FIRST_DELTA_BUDGET_MS` override the budgets for a measuring run.

## Reading Datadog from Genus

Several tenants can share one Datadog account. Genus reads it with one
server-held, read-only credential and lets each tenant read only its own
telemetry: every query Genus sends is `(<tenant scope>) AND (<caller query>)`.
The package is `robothor/integrations/datadog/`; agent tools and dashboard views
build on it.

### Settings

| Setting | What it is |
|---------|------------|
| `ROBOTHOR_DATADOG_API_KEY` | Secret. The API key, sent as `DD-API-KEY`. |
| `ROBOTHOR_DATADOG_APP_KEY` | Secret. The application key, sent as `DD-APPLICATION-KEY`. Give it read scopes only. |
| `ROBOTHOR_DATADOG_SITE` | The Datadog site, default `datadoghq.com`. Must be a known site (`us3.datadoghq.com`, `us5.datadoghq.com`, `datadoghq.eu`, `ap1.datadoghq.com`, `ap2.datadoghq.com`, `ddog-gov.com`, `us2.ddog-gov.com`); anything else reads as not configured, so the keys are never sent to another host. |
| `ROBOTHOR_DATADOG_SCOPES` | JSON: what each tenant may read. |

The two keys are one credential for the whole instance, not per tenant. They
resolve from the vault first, always from the root tenant's rows, and then from
the environment. Use the keys of a Datadog service account that can only read.

### The scope map

```json
{
  "acme-parent":  {"logs": "*", "metric_tags": []},
  "acme-child-a": {"logs": "service:shop-api"},
  "acme-child-b": {
    "logs": "service:clinic-app",
    "spans": "service:clinic-app env:prod",
    "monitors": "team:clinic",
    "metric_tags": ["service:clinic-app"]
  }
}
```

- `logs` is required. It is a Datadog log query that bounds the tenant. Only
  the literal `*` means everything; a blank filter is not configured.
- `spans` defaults to the `logs` filter.
- `monitors` and `metric_tags` default to no access. `metric_tags: []` means
  every metric.
- **A tenant with no entry is refused**, and so is a tenant whose entry is
  malformed (a blank `logs`, unbalanced brackets, a tag with a comma or space).
  Malformed JSON leaves every tenant refused. Each case logs one warning that
  names the tenant, never the entry.

A caller's query is refused before anything is sent when it could change what
the wrapping means: unbalanced or out-of-order brackets or parentheses, an
unclosed quoted phrase, a backslash in a phrase or before a bracket or quote,
an unpaired single quote outside double quotes, control characters or line
breaks, or more than 4000 characters. Brackets inside a double-quoted phrase are
literal. The refusal is one plain sentence the caller can act on, for instance
to put text containing an apostrophe in double quotes.

### What each part of the scope bounds

| Part | Bounds | How |
|------|--------|-----|
| `logs` | Log search and log counts | `(<logs>) AND (<caller query>)` |
| `spans` | Span search, span counts and single traces | `(<spans>) AND (<caller query>)` |
| `monitors` | Monitor search | `(<monitors>) AND (<caller query>)`, in monitor search syntax (`tag:"team:shop"`, `status:alert`, free text) |
| `metric_tags` | Metric queries | The tags lead the metric's tag filter, ANDed with the caller's |

The bracket and quote rules above apply to the `monitors` filter and to a
caller's monitor query as well. A tenant with no `monitors` filter, or no
`metric_tags`, is refused before anything is sent; it never gets a query that
merely matches nothing.

**A trace from another tenant reads as not found.** Datadog has no way to fetch
a trace by id inside a filter, so Genus searches the tenant's spans for
`trace_id:<id>`. A trace outside the tenant's `spans` filter matches no span.
The id must be hex or decimal digits, so it cannot carry search syntax.

**Metric queries are built from parts, never from query text.** The caller
names a metric, an aggregator (`avg`, `sum`, `min` or `max`), optional tags,
optional group-by keys, and whether to count. Genus builds
`<aggregator>:<metric>{<scope tags>,<caller tags>} by {<keys>}`, using `{*}`
only when the scope is `metric_tags: []` and the caller adds no tags. A metric
name, tag or key containing a space, comma, brace or quote is refused. The time
window is ISO-8601, `now`, or `now-<n>` with `s`, `m`, `h` or `d`.

### Doctor

`genus doctor` runs `datadog.reachable`. It skips when the keys are not set,
and under `--offline`. Otherwise it asks Datadog to validate the API key, then
makes one log search that matches nothing, because only that second call checks
the application key and its log read access. It fails when the site is unknown,
when Datadog rejects either key (it never prints a key), or by error kind when
Datadog cannot be reached. Its detail names the
active tenants that have no scope entry, whose reads are refused until you add
one.

### Genus watches Datadog

`robothor/engine/datadog_watch.py` runs four read-only observers from the
watchdog, every 5 minutes beside the failure-mode detectors. Each pass runs as
a background task, so a slow Datadog never holds the watchdog loop, and a tick
that finds the previous pass still running skips. They only flag: nothing they
find stops, pauses or limits anything. Each finding is sent once to the
tenant's main agent, through its alert digest, and recorded with what was sent
(see [Who hears about a finding](#who-hears-about-a-finding)).

| Observer | Looks at | Finds |
|----------|----------|-------|
| Error spike | `status:error` log counts per service, inside the tenant's `logs` scope | a service whose errors in the latest 15 minutes are well above its own last two hours |
| Silent service | every log line counted per service, inside the tenant's `logs` scope | a service that logged steadily for two hours and logged nothing in the latest 15 minutes (`service_silent`); its next line is `service_resumed` |
| Firing monitors | the monitors inside the tenant's `monitors` scope in Alert, Warn or No Data, and not muted | a monitor that starts firing or changes status (`monitor_firing`), and one recorded that stops (`monitor_recovered`) |
| Connection lost | the API key check (`/api/v1/validate`), an application key probe (a one-row log search that matches nothing), and the watch's own reads | Datadog rejecting either key, or unreachable, on 3 checks in a row; the first good check after that is `connection_restored` |

**The spike rule.** The current window is the latest complete 15-minute slot
that ended at least 2 minutes ago; the baseline is the 8 slots before it. A
service fires when its current count is at least 20 **and** above both 3× its
baseline median and the median plus 5 robust deviations (scaled median
absolute deviation), so a noisy service needs a bigger jump than a steady one.
A service whose baseline median is zero (new, silent, or a stray error or two)
fires only at 100, so there is no jump between "silent" and "one stray error".
No per-service tuning.

**The silence rule.** The same slots, counting every line rather than errors.
A service is silent when the current slot holds no lines from it **and** it
logged in every one of the 8 slots before **and** its median over those slots
is at least 15 lines (one a minute), so a service that logs every minute is
watched. The floor keeps a service that logs only now and then (a heartbeat
every quarter of an hour, say) from counting one late line as a silence; the
"every slot" condition keeps a service with gaps (a nightly job), or a new one
without two hours of history, out. A silence is
recorded once; `service_resumed` is recorded when the service logs again, and
names the slot it went quiet in. Logs that carry no service tag are never a
silent service. If a tenant logs from 1000 or more services in one slot (one
page of groups), silence is not judged for it, because a service missing from
a full page may simply not fit; one log line says so.

**The monitor rule.** Every pass lists the tenant's monitors that are firing:
the search is `(<monitors scope>) AND ((status:alert OR status:warn OR
status:"no data") AND muted:false)`, read page by page (100 per page) until the
last. A muted monitor, muted by hand or by a scheduled downtime, is left out,
because Datadog holds back its own notification for it. A monitor is recorded
when it starts firing, and again only when it changes status while firing (Warn
to Alert, say) or after it recovers and fires again. One that was recorded and
is no longer in the list is `monitor_recovered`, whether it stopped firing or
was muted (the brief says "no longer firing, or has been muted"). A read that fails records no
recovery: the next good read decides. A tenant whose `monitors` scope is
missing or "no access" skips this observer only (one log line per process) and
its logs are still watched. A key without monitor read permission is not a lost
link: the monitor watch skips that pass and the log observers carry on. The
brief names the monitor (email addresses and phone numbers in the name are
replaced), its status, and how long since it last triggered.

Datadog's own notifications, wired to Genus's webhook, are handled separately
as `datadog.alert_received` ([Datadog alerts into Genus](#datadog-alerts-into-genus)).
This observer is the safety net for monitors that are not wired to that
webhook, so a monitor that is wired appears in both records but is sent once:
see [No double notification](#who-hears-about-a-finding) below.

**Calls per pass.** Each settled slot is read once and kept, per series. A
tenant costs 18 aggregate calls on the first pass after a restart (9 slots for
errors, 9 for every line) and then two per new slot (about two every 15
minutes), plus one monitor search per 100 firing monitors on every pass (one
when none fire). The root engine adds the two key checks per pass.

**Where it runs.** Each engine watches its own tenant (`ROBOTHOR_TENANT_ID`), so
every tenant with an engine is covered once. A service inside two tenants'
scopes (a parent scoped `*` sees its children's services) is watched by both,
and a spike there is one finding in each. The engine whose tenant has no parent in
`crm_tenants` also owns the connection check, so a lost key is one finding, not
one per engine. With HA leader election only the scheduler leader watches. The
watch does nothing while `ROBOTHOR_DATADOG_SCOPES` is empty, or while the keys
are unset (one log line says so), and a tenant with no scope entry is skipped
with one log line per process. `ROBOTHOR_DETECTORS_ENABLED=0` stops it with the
other detectors.

**Where findings appear.** Each finding is one `datadog.watch_finding` audit
event in the tenant it concerns (the connection findings in the root tenant)
with `kind` (`error_spike`, `service_silent`, `service_resumed`,
`monitor_firing`, `monitor_recovered`, `connection_lost`,
`connection_restored`), `service`, `current`, `baseline`, `window`, `mode`,
`delivery` and `brief`, the two or three sentences that were sent. A
monitor finding carries `monitor` (`id`, `name`, `type`, `status`,
`previous_status`) in place of the counts, and the audit target
`monitor:<id>`. Briefs and events carry counts, service names and monitor
names only, never a log message or a monitor's notification text. Each finding also writes one log
line:

```
event=datadog_watch tenant=acme-child-a kind=error_spike service=shop-api mode=delivered
```

#### Who hears about a finding

The engine that found it delivers it, then records it: the watch
only ever watches its own engine's tenant, and the alerts path
(`robothor.engine.alerts.alert`) delivers in the process's own tenant. The
brief is sent once through that path, as the body of an alert titled after the
finding (`Datadog error spike: shop-api`), and then the finding is recorded
with the outcome. Nothing the watch finds is `critical`, so nothing pages:
warning and info both become an `alert_digest` row in the main agent's inbox,
which its heartbeat and the morning briefing read.

| Finding | Level |
|---------|-------|
| `connection_lost`, `error_spike`, `service_silent` | warning |
| `monitor_firing` in Alert | warning |
| `monitor_firing` in Warn or No Data | info |
| `monitor_recovered`, `service_resumed`, `connection_restored` | info |

**The episode rule.** One alert per episode. An episode opens with a finding
and closes with its recovery (`monitor_recovered`, `service_resumed`,
`connection_restored`, each sent at info) or, for a spike, with the first pass
in which the service is no longer spiking (nothing is sent for that). A spike
that goes on is recorded again each hour but not sent again; a monitor that
moves to a higher level inside its episode (Warn to Alert) is sent again at
that level, and one that falls back (Alert to Warn) is not. Each alert carries
an id per episode phase (`datadog_watch:<tenant>:<subject>:open:<level>` or
`:close`), so the alerts cooldown (`ROBOTHOR_ALERT_COOLDOWN_SECONDS`, an hour
by default) also holds a monitor that flaps: a second opening inside the
cooldown is recorded and not sent. Episodes live in the engine process, so a
restart forgets them (see below).

**`mode` and `delivery`.** `mode` is what the delivery did:

- `delivered`: the alerts path confirmed it. `delivery` holds `level`,
  `route` (`digest`) and `alert_id`.
- `suppressed`: not sent, because its episode already was
  (`reason: episode_open`) or the cooldown holds its id (`reason: cooldown`).
- `delivery_failed`: the alerts path raised (`error` is the exception's
  class name) or reported that it delivered nothing (`error: not_delivered`),
  or the finding belongs to another tenant than the engine's
  (`reason: not_this_engine`, which a normal pass never produces). The finding
  is recorded all the same and the pass goes on; the next finding of that
  episode is tried again.

Records written before delivery existed say `mode: dry_run` and carry
`would_notify` instead of `delivery`. Read anything that is not `delivered`
as recorded and not sent. There is no setting for any of this; to stop
delivery, revert the change that added it.

Each observer runs on its own for each tenant: one failing (a Datadog error, a
malformed scope) never stops another observer or another tenant.

A continuing spike is recorded once an hour per service and sent once per
episode. A lost link is recorded once, until it is restored. All of this
state, episodes included, lives in the engine process, so a restart forgets it
(a finding after a restart opens a new episode and is sent):

- a restart while the link is down records the loss again after 3 more failed
  checks, and one just as the link comes back records no `connection_restored`;
- every monitor still firing is recorded again on the first pass after a
  restart, and a monitor that recovered while the engine was down gets no
  `monitor_recovered`;
- a service silent across a restart is not recorded again (its baseline now
  holds the silent slots), and gets no `service_resumed` when it comes back.

**Is it running?** Look for `event=datadog_watch` lines, or for
`datadog.watch_finding` rows in the audit log. If there are none, check that
`ROBOTHOR_DATADOG_SCOPES` has an entry for the engine's tenant and that
`genus doctor --only datadog.reachable` passes. A `datadog watch: not running`
line names the reason.

### Datadog alerts into Genus

When a monitor in the shared account changes state (alert, warn, no data,
recovered), Datadog can call Genus. Genus works out which tenant the monitor
belongs to, delivers the alert in that tenant through that tenant's own
engine, starts one read-only triage run of that tenant's main agent when the
monitor triggers, and records what it did.

**1. The webhook channel.** Add a channel to the webhook receiver's channels
(`docs/webhooks.yaml`, a `docs/webhooks.d/` drop-in (both instance files), or the chart's
`hooks.channels`; see [Webhook ingress](event-bus.md#webhook-ingress)). The
stream must be `datadog`:

```yaml
channels:
  datadog:
    stream: datadog
    secret_env: DATADOG_WEBHOOK_SECRET
    signature_scheme: header_secret
    signature_header: X-Genus-Webhook-Secret
    event_type_prefix: datadog.alert
```

Datadog cannot sign a webhook body, so the `header_secret` scheme checks a
shared secret that Datadog sends in a custom header. The comparison runs in
constant time. Generate a long random value and put it in the receiver's
environment under the `secret_env` name. A wrong or missing header gets
`401`. The receiver logs one warning naming the channel and the reason
(`secret mismatch` or `missing secret header`), and never logs the value
sent. Nothing is published or recorded. If the variable is not set, every
delivery gets `503`.

**2. The Datadog webhook.** In Datadog's Webhooks integration, add a webhook
whose URL is the receiver's public address followed by `/api/webhooks/datadog`.
Set **Custom Headers** to
`{"X-Genus-Webhook-Secret": "<the secret>"}`, and set the **Payload** to:

```json
{
  "monitor_id": "$ALERT_ID",
  "event_id": "$ID",
  "title": "$ALERT_TITLE",
  "transition": "$ALERT_TRANSITION",
  "status": "$ALERT_STATUS",
  "priority": "$ALERT_PRIORITY",
  "query": "$ALERT_QUERY",
  "tags": "$TAGS",
  "link": "$LINK",
  "date": "$DATE",
  "hostname": "$HOSTNAME"
}
```

Then mention the webhook (`@webhook-<its name>`) in each monitor's message.
Genus parses the body defensively. A missing field is read as empty, a body
that is valid JSON but not an object is recorded with empty fields, and a
body that is not JSON gets `400`. `tags` may be the comma-separated `$TAGS`
string or a JSON list. `monitor_id` falls back to an `alert_id` field.

**3. The `org:` tag decides the tenant.** Tag each monitor with
`org:<tenant id>`. Only the tags are read for tenancy, never any other
field.

| Tags | Recorded in | `routing` |
|------|-------------|-----------|
| `org:` names an active tenant | that tenant | `org` |
| no `org:` tag | the root tenant (`ROBOTHOR_DEFAULT_TENANT`) | `untagged` |
| `org:` names no active tenant | the root tenant | `unknown_org:<value>` |
| two different `org:` tags | the root tenant | `ambiguous_org:<a>,<b>` |

`$TAGS` also carries the tags of the group that triggered a multi-alert
monitor. Keep `org:` off your hosts and services, so that only the monitor
sets it.

**4. Who delivers it.** Every engine reads the `datadog` stream once
`ROBOTHOR_DATADOG_SCOPES` is set. A webhook carries no tenant, so the root
tenant's engine (the one on the `engine` group) consumes it and routes it:

- routed to the root tenant (its own, or untagged, unknown or ambiguous):
  the root engine delivers it itself;
- routed to another tenant: the root engine delivers nothing. It writes one
  hand-off event to the same stream, type `datadog.deliver`, source
  `datadog_handoff`, addressed to that tenant (`tenant_id`), which only that
  tenant's engine acts on (every other engine drops an event addressed to
  another tenant). That engine routes the alert again from its own tags and
  delivers it only when the answer is an `org:` tag naming itself; anything
  else is refused, logged, and neither recorded nor sent. It records the alert
  in its own tenant.

The alerts path only delivers in the process's own tenant, so an alert is
never delivered or triaged in a tenant other than the one its tags name.

**When the hand-off cannot be made.** Before handing off, the root engine
checks that the tenant's engine read the stream in the last minute (a
consumer named `engine-<tenant>` in any group). When none did, or the hand-off
cannot be written, the alert is recorded in that tenant with
`mode: delivery_failed` and `delivery.reason` `no_engine` or
`handoff_failed`, no run starts, and the root engine sends one `info` alert,
`Datadog alert not delivered: tenant <id>`, to its own alert digest. It names
the tenant and nothing of the alert, and its alert id
(`datadog_undelivered:<tenant>`) keeps it to one per tenant per cooldown.
Start that tenant's engine with `ROBOTHOR_DATADOG_SCOPES` set to fix it; an
alert already recorded this way is not sent later.

**Levels.** `triggered` and `re_triggered` are `critical`: they page the
tenant engine's operator on Telegram and then each
`ROBOTHOR_ALERT_FALLBACK_CHANNELS` channel. Warn and no-data transitions are
`warning`, recoveries are `info`; both become an `alert_digest` row in the
main agent's inbox. The alert's title is `Datadog monitor: <title>` and its
body names the monitor (title masked, id), its state and a `P1`-`P5`
priority. Never the query, the message or a log line.

**The episode rule.** A monitor's notifications form an episode that its
recovery closes. Inside one, each level is sent once: a renotification, a
`Re-Triggered`, a `Re-Warn` or a storm of one monitor's groups is recorded and
not sent (`mode: suppressed`, `reason: episode_open`), a Warn that escalates
to Alert is sent again at `critical`, and a fall back to Warn is not. The
recovery is sent once at `info` and closes the episode, so the next trigger is
a new one. Episodes are kept in Redis
(`robothor:datadog:episode:<tenant>:<monitor>:*`) for seven days, so a
recovery Datadog never sent cannot silence a monitor for good. The alerts
cooldown applies on top (alert id `datadog_alert:<tenant>:<monitor>:<level>`).
A send that fails is recorded as `delivery_failed` (with `error`) and the next
notification of the episode tries again.

**The triage rule.** When a `Triggered` or `Re-Triggered` alert is delivered
(never Warn, No Data or a recovery, and never a suppressed or failed one), the
tenant's engine starts ONE run of its main agent for the episode. The run is
in plan mode (`readonly_mode`): it is offered only its read-only tools, the
runtime admission gate refuses any other, and its prompt is fixed: it names
the monitor (id, masked title, time), asks for a first look with the Datadog
tools inside the tenant's scope, says to change nothing, and says to report in
one line when the agent does not have the Datadog tools. The run's result goes
out through the agent's normal delivery; nothing else is sent. The alert
records `triage` (`started`, `agent`, `read_only`, or `reason`: `not_a_trigger`,
`recovery`, `episode_open`, `no_main_agent`, `no_agent_config`, ...), and the
run's end is one `datadog.triage_run` audit event in the tenant with `status`
(the run's status, `failed` with `error`, or `agent_busy` when the main agent
was already running and no run started).

**5. What is recorded.** For each transition the engine that delivers it
writes one audit event, `datadog.alert_received`, in the routed tenant. The
event has category
`datadog`, actor `datadog` and target `datadog_monitor:<monitor id>`, and its
details hold:

- `monitor_id`, `event_id`, `title`, `transition`, `datadog_transition`,
  `status`, `priority`, `tags`, `link`, `date`. `transition` is normalised
  (`triggered`, `re_triggered`, `warn`, `no_data`, `recovered`, ...).
  `datadog_transition` is the value Datadog sent. Email addresses and phone
  numbers in the title and tags are masked.
- `routing`, from the table above.
- `mode`: `delivered`, `suppressed` or `delivery_failed`, as for the watch.
- `delivery`: `level`, `route` (`page` or `digest`) and `alert_id`, plus
  `reason` or `error` when it was not sent.
- `triage`: as above.

Records written before delivery existed say `mode: "dry_run"` and carry
`would_notify` and `would_run`. Read anything that is not `delivered` as
recorded and not sent.

A recovery changes nothing in place. It is its own event with
`transition: recovered` and the same `monitor_id`, so you can pair it with
the trigger. Datadog retries webhooks, so the same notification (`event_id`,
monitor, transition and date) is recorded and delivered once, and a hand-off
read twice is delivered once. Each group of a multi-alert
monitor has its own `event_id`, so each is recorded. Without `event_id` and
`date`, one record is kept per monitor and transition. The key is set in
Redis for 24 hours, and only after the audit event is written. A crash
between the write and the key, or two copies handled at the same moment, can
therefore record a retry twice, but an alert is never lost. If Redis cannot be
reached the alert is still recorded. When the audit write fails, no key is
set and the event goes to `robothor:events:datadog:dlq`.

Each recorded alert also writes one log line with no title or body:
`event=datadog_alert tenant=<tenant> monitor_id=<id> transition=<transition> mode=<mode> routing=<routing>`.
A hand-off writes the same line on the root engine with `mode=handed_off`.

To see the records, open the Helm's audit log, or call the bridge's
`GET /api/audit/events?event_type=datadog.alert_received`. Both read
`robothor.audit.logger.query_log`, filtered to the tenants the caller may see.

**6. Rolling back.** There is no setting that turns delivery or the triage
run off, and no dry-run mode. To go back to recording only, revert the change
that added delivery (`robothor/engine/datadog_delivery.py` and its callers in
the watch and the stream handler); records written meanwhile stay valid.

## Notes

- Export is best-effort (a 5s-timeout POST per run); a collector outage never
  blocks a run.
- The payload builder is `TraceContext.build_otlp_payload()`; GenAI attributes
  are built by `telemetry.gen_ai_attributes()` and attached at the runner's
  `llm_call` span.
