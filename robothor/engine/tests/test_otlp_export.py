"""Full OTLP export with span attributes + GenAI conventions (Wave-2, W2-7).

The prior exporter shipped only span status — no attributes, events, or
durations. build_otlp_payload now serializes attributes (incl. gen_ai.*) and
durations. No new dependency: it enriches the existing hand-rolled OTLP/HTTP
payload (httpx), so it works against Jaeger/Tempo/Datadog OTLP intake.
"""

from __future__ import annotations

import pytest

from robothor.engine.telemetry import (
    TraceContext,
    _otlp_value,
    cache_hit_ratio,
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


def test_gen_ai_attributes_omits_cache_keys_when_absent():
    """Backward-compat: no cache activity → no cache attributes added."""
    attrs = gen_ai_attributes(model="anthropic/claude-opus-4-8", input_tokens=100)
    assert "gen_ai.usage.cache_read_input_tokens" not in attrs
    assert "gen_ai.usage.cache_creation_input_tokens" not in attrs
    assert "gen_ai.usage.cache_hit_ratio" not in attrs


def test_gen_ai_attributes_includes_cache_tokens_and_ratio():
    """PR 4: cache-hit-rate metrics (observe-only) on GenAI span attributes."""
    attrs = gen_ai_attributes(
        model="anthropic/claude-opus-4-8",
        input_tokens=1000,
        output_tokens=50,
        cache_read_tokens=400,
        cache_creation_tokens=100,
    )
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 400
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 100
    assert attrs["gen_ai.usage.cache_hit_ratio"] == pytest.approx(0.4)


def test_gen_ai_attributes_cache_creation_only_still_adds_ratio():
    """Cache-write-only calls (first turn, nothing to read yet) still get a
    ratio attribute (0.0) since cache activity occurred."""
    attrs = gen_ai_attributes(
        model="anthropic/claude-opus-4-8",
        input_tokens=1000,
        cache_creation_tokens=200,
    )
    assert attrs["gen_ai.usage.cache_hit_ratio"] == 0.0


class TestCacheHitRatio:
    """Pure hit-ratio math, including the zero-prompt-token edge."""

    def test_basic_ratio(self):
        assert cache_hit_ratio(cache_read_tokens=400, prompt_tokens=1000) == pytest.approx(0.4)

    def test_zero_prompt_tokens_does_not_divide_by_zero(self):
        assert cache_hit_ratio(cache_read_tokens=0, prompt_tokens=0) == 0.0

    def test_zero_prompt_tokens_with_nonzero_cache_read_clamped(self):
        # Degenerate: cache_read > prompt_tokens shouldn't happen in practice,
        # but the math must not raise (denominator floors at 1) and the result
        # must stay a ratio — clamped to a documented max of 1.0.
        assert cache_hit_ratio(cache_read_tokens=5, prompt_tokens=0) == 1.0

    def test_cache_read_exceeding_prompt_tokens_clamped_to_one(self):
        assert cache_hit_ratio(cache_read_tokens=1500, prompt_tokens=1000) == 1.0

    def test_no_cache_read_is_zero(self):
        assert cache_hit_ratio(cache_read_tokens=0, prompt_tokens=1000) == 0.0

    def test_full_cache_hit(self):
        assert cache_hit_ratio(cache_read_tokens=1000, prompt_tokens=1000) == 1.0


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
