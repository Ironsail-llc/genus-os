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

## Notes

- Export is best-effort (a 5s-timeout POST per run); a collector outage never
  blocks a run.
- The payload builder is `TraceContext.build_otlp_payload()`; GenAI attributes
  are built by `telemetry.gen_ai_attributes()` and attached at the runner's
  `llm_call` span.
