"""Full OTLP export with span attributes + GenAI conventions (Wave-2, W2-7).

The prior exporter shipped only span status — no attributes, events, or
durations. build_otlp_payload now serializes attributes (incl. gen_ai.*) and
durations. No new dependency: it enriches the existing hand-rolled OTLP/HTTP
payload (httpx), so it works against Jaeger/Tempo/Datadog OTLP intake.
"""

from __future__ import annotations

from robothor.engine.telemetry import (
    TraceContext,
    _otlp_value,
    gen_ai_attributes,
)


def test_otlp_value_types():
    assert _otlp_value(True) == {"boolValue": True}
    assert _otlp_value(5) == {"intValue": "5"}
    assert _otlp_value(1.5) == {"doubleValue": 1.5}
    assert _otlp_value("x") == {"stringValue": "x"}
    assert _otlp_value(["a"]) == {"arrayValue": {"values": [{"stringValue": "a"}]}}


def test_gen_ai_attributes():
    attrs = gen_ai_attributes(
        model="anthropic/claude-opus-4-8",
        input_tokens=100,
        output_tokens=20,
        finish_reason="stop",
    )
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "anthropic/claude-opus-4-8"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 20
    assert attrs["gen_ai.response.finish_reasons"] == ["stop"]


def test_payload_includes_attributes_and_duration():
    trace = TraceContext(run_id="r1", agent_id="a1")
    with trace.span("llm_call", **gen_ai_attributes(model="gpt-5.5", input_tokens=7)):
        pass

    payload = trace.build_otlp_payload({})
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "llm_call"
    assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"]) > 0
    keys = {a["key"] for a in span["attributes"]}
    assert "gen_ai.request.model" in keys
    assert "gen_ai.usage.input_tokens" in keys
    # resource carries service.name
    rattrs = {a["key"] for a in payload["resourceSpans"][0]["resource"]["attributes"]}
    assert "service.name" in rattrs


def test_error_span_status_code_2():
    trace = TraceContext(run_id="r1", agent_id="a1")
    try:
        with trace.span("boom"):
            raise ValueError("x")
    except ValueError:
        pass
    span = trace.build_otlp_payload({})["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["status"]["code"] == 2
