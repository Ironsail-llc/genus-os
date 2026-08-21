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

Per `llm_call` span (GenAI conventions):
- `gen_ai.system` (anthropic / openai / google / …)
- `gen_ai.request.model`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons`

Resource attributes: `service.name=robothor-engine`, `agent.id`. Span status uses
OTLP codes (1 = ok, 2 = error). Run metrics are also published to the Redis
stream `robothor:events:telemetry` for the dashboards.

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
| `runaway_burn_detector` | 2 min | a running run has crossed 500K tokens |
| `zombie_runner_detector` | 10 min | `running` >15 min with no step activity for 5 min |
| `stuck_workflow_detector` | 10 min | `workflow_runs` running past timeout + grace |
| `workflow_failure_streak_detector` | 10 min | the same workflow's last 3 terminal runs all failed |

Disable all of them with `ROBOTHOR_DETECTORS_ENABLED=0`.

### Why two tool detectors

`tool_degradation_detector` looks at one hour and needs ~5 failures in it, so it
only sees *acute* breakage on a busy tool. A tool called twice a day can be 100%
dead forever without ever putting 5 failures in the same hour — which is how
`apollo_search_people` failed 32/32 (`error_type=auth`) across 14 days with
nothing alerting. `tool_outage_detector` trades resolution for reach: a 7-day
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

## Notes

- Export is best-effort (a 5s-timeout POST per run); a collector outage never
  blocks a run.
- The payload builder is `TraceContext.build_otlp_payload()`; GenAI attributes
  are built by `telemetry.gen_ai_attributes()` and attached at the runner's
  `llm_call` span.
