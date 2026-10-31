# Performance

How long a chat turn takes, where that time goes, how to measure it, and the
switches that make it shorter. Read this before turning a performance setting on
or off, and keep the numbers from before and after.

Several pieces described here are arriving in separate pull requests. Where one
has not merged yet, its section says **pending**: the setting does nothing on a
release that does not carry it.

## How a turn is timed

A turn is one message in and one answer out. The engine spends it in these
phases, in this order:

| Name | What it covers | Where you read it |
|---|---|---|
| **SETUP** | From the run starting to the agent being ready to call a model: manifest load, warmup preamble (identity, memory blocks, entity context, open tasks), system prompt. | The runner's `SETUP <n>ms agent=... trigger=...` log line (`robothor/engine/runner.py`). |
| **Setup plus toolset** | SETUP and building the tool list the model is offered. | The run's `tools_built` step. Each warmup section is its own `warmup_section:<name>` step. |
| **TTFT** (time to first token) | Message sent to the first streamed text (`delta`) the person sees. A turn that does not stream (Telegram today) has no first token; its equivalent is the first model call returning. | The webchat SSE stream; the `llm_call` span. |
| **done** | Message sent to the `done` event (webchat) or the reply being sent (Telegram). This is the time the person waits. | The `done` event's `duration_ms`; the `agent_run` span. |
| **Finalize tail** | The gap between the last text and `done`: telemetry, post-run bookkeeping, the run-row write, history save. It is pure waiting for the person. | `done` minus the last `delta`. |
| **Tool time** | The tool handlers' own time. | The run's tool-call steps; `tool_call` spans. |
| **Tool-call overhead** | For each gap between two model calls with tool calls in it: the gap minus the tools' own time. What the loop spends around a tool (admission, audit, guardrails, result shaping). | Derived from the run's steps. |
| **DB statements** | Statements the engine sent to Postgres for the turn, including the background persist. | `pg_stat_statements`, before and after the turn. |
| **Prompt tokens** | Size of the first model call: messages plus tool schemas. Most of a large prompt is usually tool schemas. | The first `llm_call` span's `gen_ai.usage.input_tokens`; the benchmark's estimate. |
| **Cache hit ratio** | Share of input tokens the provider served from its prompt cache. | `gen_ai.usage.cache_hit_ratio` (with the cache read and creation token counts) on the run's `run_summary` span; `cache_hit_ratio` on the telemetry stream. Per-call `llm_call` spans do not carry it. |

Engine restart to `/ready` is measured separately: it is the cold-start time an
operator waits after an upgrade, not part of any turn.

## The turn benchmark (`bench/turn`)

*Pending: arrives with the per-turn benchmark pull request. Its own
`bench/turn/README.md` is the reference for every option.*

`python -m bench.turn` drives whole turns against a running stack on the mock
model and records every number in the table above, per turn. It has two halves,
chosen with `--half http|inprocess|both`:

| Half | What it drives | What it records |
|---|---|---|
| `http` | N webchat turns: `POST /chat/send` on the engine, the SSE stream read as it arrives. Each turn has its own session, so each starts from an empty history. Needs an engine token. | `setup_total_ms`, `ttft_ms`, `done_ms`, `finalize_tail_ms`, `tool_ms`, `tool_overhead_ms`, `db_statements`, and with `--restart-cmd` the restart-to-ready time `engine_ready_ms`. |
| `inprocess` | N Telegram-shaped turns: `runner.execute` with no streaming callback, as the Telegram channel calls it. Runs where the engine's environment is set (inside the engine container, or CI). | `setup_ms` (the SETUP line), `setup_total_ms`, `first_llm_ms`, `done_ms`, `finalize_tail_ms`, `tool_ms`, `tool_overhead_ms`, `db_statements`, `prompt_tokens` (characters / 4, stable rather than exact) and `tool_schemas`. |

The first (cold) turn is a warmup and is kept out of the summary. Each run prints
p50 and p95 per metric and, with `--out FILE`, writes the JSON result. With
`--half both` (the default) it writes one file per half, with `-http` and
`-inprocess` inserted before the extension (`before.json` becomes
`before-http.json` and `before-inprocess.json`).
`db_statements` needs Postgres started with
`shared_preload_libraries=pg_stat_statements`; without it the count is skipped
and the reason is kept in the result's `notes`. Run it on a quiet stack: other
activity on the database in the window is counted too.

### Baselines and the gate

The result is compared with a committed baseline under `bench/turn/baselines/`.
A metric whose p50 or p95 is more than `--threshold` over the baseline (default
`0.10`, 10%) **and** more than `--min-delta` in absolute terms (default 5, in the
metric's own unit, so scheduler noise on a 2 ms metric does not fail a build) is a
regression. Exit codes: 0 within threshold, 1 regression, 2 the benchmark could
not run or a turn did not complete.

| Baseline | Recorded on | Compared by |
|---|---|---|
| `http.json`, `inprocess.json` | The local stack, mock model | You, before and after a change |
| `ci-inprocess.json` | The `bench-turn` job in CI | That job, on every pull request touching `robothor/engine/`: 20 in-process turns against Postgres with `pg_stat_statements`, Redis and the mock model. A regression fails the pull request. |
| `ci-http.json` | The install gate's compose job | That step, nightly, report only |

A committed baseline starts as an empty sentinel, against which comparison prints
`no baseline recorded` and passes. `--update-baseline` writes a real one; commit
it in its own change so the number has a history.

### The before/after loop for a setting

1. Stack up on the mock model, setting off. Run one half at a time, for example
   `--half inprocess --out before.json`.
2. Turn the setting on (restart the engine if its row below says so).
3. Run the same half again with `--out after.json`.
4. Compare: `python -m bench.turn --compare after.json --baseline before.json`.
   Put both p50/p95 tables in the pull request or the rollout note.

Name the files exactly. If you ran `--half both`, compare each half's files on
both sides (`--compare after-inprocess.json --baseline before-inprocess.json`):
`--compare` does not add the suffix to `--baseline`, and a baseline file that
does not exist is treated as an empty baseline, which prints
`no baseline recorded: comparison skipped` and exits 0 without comparing
anything. That line means the comparison did not happen.

## Engine telemetry

The engine builds one trace per run and exports it over OTLP/HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set ([Observability](OBSERVABILITY.md) has the
setup). The same run metrics go to the Redis stream `robothor:events:telemetry`
for the dashboards.

*Pending, with the off-loop telemetry pull request:* the root span, span kinds,
export on every exit path, the resource settings and the off-loop publish below.
Until then a trace has no root span and only successful runs export.

| Span | Kind | Covers | Attributes worth reading |
|---|---|---|---|
| `agent_run` | SERVER | The whole run, from its `started_at` to its exit. **Its duration is the turn time.** A sub-agent's `agent_run` is a child of its parent run's span. | `run.status`, `run.trigger_type`; error status and the reason on a failed, timed-out, cancelled, aborted or interrupted run. |
| `llm_call` | CLIENT | One model call, including its retries on the same model. | GenAI conventions: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`. |
| `tool_call` | INTERNAL | One tool handler. | `tool` (the tool's name); error status when the handler raises. |
| `run_summary` | INTERNAL | A marker at run end carrying the run's totals. | `run.status`; the run's model and token totals, including `gen_ai.usage.cache_hit_ratio` and the cache read and creation tokens. |

A backend that names operations by span kind (Datadog's OpenTelemetry mapping, for
one) shows `server.request` for `agent_run`, `client.request` for `llm_call` and
`Internal` for the rest, with the span name as the resource. Turn-time
percentiles come from `server.request`; model latency from `client.request`; the
tool-call overhead of the table above is `agent_run` minus its `llm_call` and
`tool_call` children.

Resource attributes default to `service.name=robothor-engine` and `agent.id`.
`OTEL_RESOURCE_ATTRIBUTES` (comma-separated `key=value`, percent-decoded) is
merged over them, and `OTEL_SERVICE_NAME` overrides `service.name`, in the
OpenTelemetry order. Use them to tag the environment, for example
`OTEL_RESOURCE_ATTRIBUTES=deployment.environment=staging`, so one backend can
compare environments.

Publishing never holds up a run: the Redis entry and the OTLP export are
background tasks on one shared async Redis client and one shared HTTP client
(5 s timeout), traces finishing during an export go together in the next request,
and a sink that is down logs one warning and then stays at debug level.

## Dashboard performance

*Pending, with the dashboard vitals pull request.*

**Web vitals.** Every page load reports its Core Web Vitals (LCP, INP, CLS, FCP,
TTFB) to `POST /api/vitals`. Each metric becomes one `web_vital` JSON line on the
dashboard's standard output, for any log pipeline that collects container output,
and an entry on the capped Redis stream `robothor:events:vitals`. `rating` is
web-vitals' own `good` / `needs-improvement` / `poor`; `path` has no query string
and `view` is the dashboard view. No tenant, user or session is recorded.
`GENUS_DASHBOARD_WEB_VITALS=false` turns it off at both ends.

**Bundle budget.** `pnpm bundle:check`, after `pnpm build` in `app/`, compares
each page route's gzipped first-load JavaScript with `bundle-budget.json` (in `app/`) and
fails a route that is over its budget or has none. CI runs it after the build.
`node scripts/bundle-budget.mjs --write` resets every budget to the current size
plus 5%: raise a budget on purpose, in the pull request that causes the growth, so
the growth is a visible line in review.

**Chat perf spec.** `e2e/perf.spec.ts` in `app/` (Playwright, backends mocked) fails
when the chat takes longer than its budget to paint (5000 ms) or to show the first
streamed token after send (1500 ms), or when a tab holds more than one live
event-stream connection. `PERF_CHAT_PAINT_BUDGET_MS` and
`PERF_FIRST_DELTA_BUDGET_MS` override the budgets for a measuring run.

## Performance settings

Every setting here is off by default unless its row says otherwise, so the
default keeps the behaviour a release had before it. Turn one on in a staging or
test instance first, measure, then promote. The rollback for every one is the
same: set it back to off (`0` or `false`) and, where the restart column says so,
restart the service. None of them migrates data, so turning one off leaves
nothing behind.

| Setting | Area | Default | Restart | Status |
|---|---|---|---|---|
| `ROBOTHOR_CONFIG_CACHE` | Manifest and adapter config | off | no (read on every call) | pending |
| `ROBOTHOR_DB_FAST_CHECKOUT` | Database checkout | off | engine and bridge | pending |
| `ROBOTHOR_WARMUP_PARALLEL` | Warmup preamble | off | engine | pending |
| `ROBOTHOR_EARLY_DONE` | Answer before finalization | off | engine | pending |
| `ROBOTHOR_CACHE_LAYOUT_V2` | Provider prompt cache | off | engine | pending |
| `ROBOTHOR_ROUTER_V2` | Planner call on long chat turns | off | engine | pending |
| `ROBOTHOR_COMPACTION_V2` | Context compaction | off | no | pending |
| `ROBOTHOR_RIP_16_ENABLED` | Deferred tool loading | off today; on once its pull request lands | engine | shipped (off); default change pending |
| `ROBOTHOR_CHANNEL_STREAMING` | Streaming to Telegram and plugin channels | off (planned) | engine | pending, no pull request yet |
| `ROBOTHOR_EXECUTION_LANES` | Interactive and background lanes | off | engine (the thread split is installed at start) | pending |
| `ROBOTHOR_STEER_EVERYWHERE` | Steering a live run from every channel | off (planned) | engine | pending, no pull request yet |
| `ROBOTHOR_AMBIENT` | Ambient follow-ups | off (planned) | engine | pending, no pull request yet |

Each setting's full description is in the
[settings reference](reference/configuration.md) once it ships. The names of the
three rows without a pull request yet may change when their pull requests open.

### `ROBOTHOR_CONFIG_CACHE`

- **Changes:** a run reuses the agent's merged and validated manifest, its parsed
  adapter configs and the sha256 of each pinned adapter binary, instead of
  reading, merging, validating and hashing them before every run. An entry is
  used only while every file it came from is unchanged (inode, size, mtime,
  ctime), the `ROBOTHOR_*` environment and the `${VAR}` values it names are
  unchanged, and no plugin sync, plugin reload or defaults reload has run since.
  Schema validation in `observe` and `enforce` still counts and refuses on every
  load. With it on, an edited adapter YAML reaches runs within one
  extension-watcher poll (10 s); `POST /api/extensions/reload` applies it at once
  ([Connectors](CONNECTORS.md)).
- **Default:** off.
- **Measure:** the benchmark's `setup_ms` and `setup_total_ms`. A warm manifest
  load goes from about 1 ms to about 0.15 ms on a small manifest; the saving grows
  with the number of adapters and pinned binaries.
- **Rollback:** off. Every load parses again from the next run.

### `ROBOTHOR_DB_FAST_CHECKOUT`

- **Changes:** a database checkout no longer starts a thread; the tenant binding
  (`app.tenant_id`) and the vector-scan setting are sent only when they differ
  from what the pooled connection already carries (a connection reused for another
  tenant is always re-bound); the hottest pure reads run without a transaction. A
  repeat single-statement read costs 1 round trip instead of 5, a write 3.
- **Default:** off.
- **Not valid behind a transaction-mode connection pooler.** The memo needs
  session-preserving connections: direct PostgreSQL or session pooling. It also
  drops the fixed 10-second cap per checkout: a checkout that has to open a
  connection is bounded by `ROBOTHOR_DB_CONNECT_TIMEOUT` instead, which must stay
  above 0 with this on. `docs/runbooks/TENANT_RLS.md` has the detail.
- **Measure:** the benchmark's `db_statements` per turn, and `setup_ms`.
- **Rollback:** off, and restart the engine and the bridge.

### `ROBOTHOR_WARMUP_PARALLEL`

- **Changes:** the interactive warmup preamble's sections (identity, memory
  blocks, entity context, context hooks, host state, open tasks, fleet surfaces,
  agent goal) are built concurrently on a small bounded pool and joined in the
  same order, so the text is identical and the first turn waits for the slowest
  section instead of the sum. Memory blocks are read in one statement, entity
  candidates in one query, and identity enrichment is held per chat session for
  up to 15 minutes (a change of who the caller resolves to recomputes it; a CRM
  edit to that person can lag by up to 15 minutes).
- **Default:** off.
- **Measure:** `setup_ms` and `db_statements`. Each section's time is on every
  run as a `warmup_section:<name>` step, with the flag off too, so record the
  per-section numbers before turning it on; with it on, the warmup should take
  about as long as the slowest section.
- **Rollback:** off, and restart the engine.

### `ROBOTHOR_EARLY_DONE`

- **Changes:** the webchat `done` event and the Telegram reply go out as soon as
  the answer's text is settled. Telemetry, post-run bookkeeping, the run-row write
  and the history save finish in the background under the engine's task
  registry, so `genus run` and engine shutdown still wait for them. A post-run
  policy that can change what the person sees (the deliverable contract in
  `enforce`) still runs first. A background failure is logged and noted on the
  run; the answer already sent stands ([Web chat](channels/webchat.md)).
- **Default:** off.
- **Measure:** `finalize_tail_ms` and `done_ms`. The tail should fall to a few
  milliseconds; the saving is whatever finalization took.
- **Rollback:** off, and restart the engine.

### `ROBOTHOR_CACHE_LAYOUT_V2`

- **Changes:** keeps the request prefix byte-stable from turn to turn so
  providers can serve it from their prompt cache. The `Current time:` line moves
  out of the system prompt into an `[engine]` turn just before the latest user
  message (the person's own text is not edited). Anthropic direct gets
  breakpoints on the last tool and on the last user or tool message before the
  final two turns; OpenAI and Gemini get no cache markers and cache the stable
  prefix themselves. `docs/MODELS.md` has the per-route table.
- **Default:** off.
- **Measure:** the cache hit ratio (`gen_ai.usage.cache_hit_ratio` on the
  `run_summary` span, `cache_hit_ratio` on the telemetry stream) and the run's
  `cache_read_tokens`, before and after; the mock model has no cache, so this one
  is measured against a real provider on a test instance. Cost per turn and TTFT
  follow it.
- **Rollback:** off, and restart the engine. The next request is laid out as
  before; the provider's cache simply misses until it refills.

### `ROBOTHOR_ROUTER_V2`

- **Changes:** on a Telegram, webchat or plugin-channel turn, message length alone
  (over 500 characters) no longer buys a separate planner model call before the
  tool loop. Such a turn plans only in plan mode, when its manifest asks for
  planning, or when the message lists three or more steps. Model selection and
  fallback chains are unchanged; scheduled, workflow, hook and sub-agent runs are
  unchanged ([Configuration](configuration.md)).
- **Default:** off.
- **Measure:** model calls per long chat turn (one fewer: the `llm_call` span
  count under `agent_run`), and `first_llm_ms` / TTFT on a long prose message.
- **Rollback:** off, and restart the engine.

### `ROBOTHOR_COMPACTION_V2`

- **Changes:** context compaction summarises with the agent's own model chain
  (manifest `model.compaction` first, the fleet chain when a caller has none),
  runs an LLM-free pre-pass first (duplicate tool results, old media, oversized
  old tool results and tool-call arguments), updates the previous summary instead
  of rebuilding it, waits `ROBOTHOR_COMPACTION_MIN_GAP_ITERATIONS` (default 3)
  between compactions, and keeps the messages, marking the run's `compaction` step
  `degraded: true`, when no model can summarise. The hard context ceiling is still
  enforced on every iteration.
- **Default:** off.
- **Measure:** model calls and wall time spent in compaction on long runs (the
  `compaction` step), prompt tokens after compaction, and how often a run's
  compaction step is degraded.
- **Rollback:** off. Read on every compaction, so no restart.

### `ROBOTHOR_RIP_16_ENABLED` (deferred tools)

- **Changes:** an agent allowed more tools than
  `ROBOTHOR_DEFERRED_TOOLS_THRESHOLD` (default 40) is offered a small core set plus
  tool search, describe and call; the rest of its allowed tools load on demand.
  Every allowed tool stays reachable. *Pending:* the change that makes this the
  default, keeps a keyword index for search, and adds a `genus doctor` check that
  every `tools_allowed` entry is either loaded or deferred.
- **Default:** off in code today; on once that pull request lands.
- **Measure:** the benchmark's `prompt_tokens` and `tool_schemas` on the first
  model call. An agent on the minimal preset sends about 216 tool schemas and
  about 37.7k prompt tokens on its first call today; that is the number this
  setting moves. Check also that turns which need a deferred tool still complete
  (one extra tool-search round trip).
- **Rollback:** `ROBOTHOR_RIP_16_ENABLED=0`, and restart the engine.

### `ROBOTHOR_CHANNEL_STREAMING`

*Pending: no pull request yet; written from the plan.*

- **Changes:** Telegram edits one message with throttled text deltas and tool
  progress lines instead of sending the answer only at the end; a transient stream
  failure retries once on the same model before falling back; plugin channels get
  the streaming callback through the shared inbound pipeline.
- **Default:** off (planned).
- **Measure:** time to the first visible text in the channel (the in-process
  benchmark's `first_llm_ms` is today's equivalent), and the model-fallback count
  on stream errors.
- **Rollback:** off; the channel goes back to one message at the end.

### `ROBOTHOR_EXECUTION_LANES`

- **Changes:** interactive turns (Telegram, the chat panel, Slack, plugin
  channels, the IDE, the CLI) and background work (cron, hooks, workflows,
  autodream, the curator, ingest, embeddings, post-turn saves) run in separate
  lanes, each with its own thread workers, so blocking background work cannot
  delay a turn's own blocking calls. Background runs are capped by
  `ROBOTHOR_MAX_CONCURRENT_BACKGROUND` (default 2). Interactive runs are uncapped
  by default, as they are without lanes (`ROBOTHOR_MAX_CONCURRENT_INTERACTIVE`,
  default 0); a cap there queues a turn with no deadline while other turns hold
  the slots, including turns waiting on an `ask_user` answer. When the
  interactive lane is capped and a turn is waiting, a background run asking for a
  slot holds back for up to `ROBOTHOR_LANE_BACKGROUND_YIELD_MS` (default 200). With two or
  more local inference slots, one is kept for interactive requests. A run waiting
  for its lane has not started: no run row, timeout or budget is touched.
- **Default:** off.
- **Measure:** `GET /health` → `execution_lanes` (`active`, `waiting`, `limit` per
  lane) and the `/metrics` gauges `robothor_lane_active{lane}` and
  `robothor_lane_waiting{lane}`. During a burst of scheduled runs, interactive
  `waiting` should stay at 0 and the benchmark's `done_ms` should not move;
  background `waiting` that never returns to 0 means its limit is too low.
- **Rollback:** off, and restart the engine (the thread split is installed at
  start).

### `ROBOTHOR_STEER_EVERYWHERE`

*Pending: no pull request yet; written from the plan.*

- **Changes:** steering a live run (a message that redirects it mid-turn) reaches
  webchat, Slack, Teams and plugin channels, not only the channels that have it
  today; tool start and end progress renders the same way on every channel; the
  dashboard's run view gets an interrupt button.
- **Default:** off (planned).
- **Measure:** time from a steer message to the run's next model call taking it
  in, and the share of interrupted runs that stop within one iteration.
- **Rollback:** off; a message sent during a run waits for the run to finish.

### `ROBOTHOR_AMBIENT`

*Pending: no pull request yet; written from the plan.*

- **Changes:** an opt-in `ambient:` manifest block lets an agent watch its
  channels and inbox streams, chase stalled threads after a set delay and post a
  digest at set times. Every message it proposes goes through the existing
  approval gate, with its tier (auto, approve, never) shown. It ships in
  `observe` first: a digest of what it would have sent, and nothing sent.
- **Default:** off (planned); `observe` when an agent opts in.
- **Measure:** it adds background work, not turn time: check the background
  lane's `waiting` and the interactive `done_ms` with it on, and the digest for
  what it would have sent.
- **Rollback:** off, or remove the `ambient:` block from the manifest.

## Operating under load: lanes, budgets that report, compaction

Three things decide whether a busy instance stays responsive and finishes its
work.

- **Lanes.** Without `ROBOTHOR_EXECUTION_LANES`, a burst of scheduled and hook runs
  shares one event loop and one thread pool with the person waiting for an answer,
  so the burst delays the answer. With it, the two lanes are rationed separately
  (see the setting above), and `/health` shows which lane is queueing.
- **Budgets that report.** *Pending, with the budgets pull request.* A spend
  budget whose limit you never set (the fleet hourly cost cap, the runaway-token
  hard cap) observes and alerts instead of stopping a run. Every budget or stop
  that refuses, ends or would have ended a run is recorded as a run step of the
  new `budget_stop` type (its database migration ships with that change) and an
  `agent.budget_stop` event, and
  `genus doctor --only budget_stops.recent` counts the last day's. The inventory
  of every budget, cap and stop, with its setting and what you see, is
  `BUDGETS_AND_STOPS.md` under `docs/` in the repository. When a run under load
  ends early, look for a `budget_stop` step before anything else.
- **Compaction.** A long run's context is compacted when it passes the proactive
  threshold. Ineffective, repeated compaction is a known way for a run to slow
  down ([Interactive request performance](runbooks/INTERACTIVE_REQUEST_PERFORMANCE.md)
  has the incident). `ROBOTHOR_COMPACTION_V2` (above) adds the LLM-free pre-pass,
  the anti-thrash gap and freeze-on-failure; the compaction section of
  `docs/SYSTEM_ARCHITECTURE.md` describes the path in full once it lands.

## Where else to look

- [Observability](OBSERVABILITY.md): enabling export, the failure-mode
  detectors, and runs in the dashboard.
- [Configuration](configuration.md) and the
  [settings reference](reference/configuration.md): every setting named here.
- [Interactive request performance](runbooks/INTERACTIVE_REQUEST_PERFORMANCE.md):
  how routine requests are kept short and bounded.
