"""Tests for structured telemetry."""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.telemetry import Span, TraceContext


class TestTraceContext:
    def test_span_creates_and_records(self):
        ctx = TraceContext(run_id="r1", agent_id="a1")
        with ctx.span("test_span", key="val") as s:
            assert s.name == "test_span"
            assert s.attributes == {"key": "val"}
        assert len(ctx.spans) == 1
        assert ctx.spans[0].duration_ms >= 0

    def test_nested_spans(self):
        ctx = TraceContext()
        with ctx.span("parent") as parent:
            with ctx.span("child") as child:
                assert child.parent_span_id == parent.span_id
        assert len(ctx.spans) == 2
        # Child is appended first (inner context exits first)
        assert ctx.spans[0].name == "child"
        assert ctx.spans[1].name == "parent"

    def test_span_error_status(self):
        ctx = TraceContext()
        try:
            with ctx.span("failing") as s:
                raise ValueError("boom")
        except ValueError:
            pass
        assert ctx.spans[0].status == "error"

    def test_to_dict(self):
        ctx = TraceContext(run_id="r1", agent_id="a1")
        with ctx.span("s1"):
            pass
        data = ctx.to_dict()
        assert data["trace_id"]
        assert data["run_id"] == "r1"
        assert data["span_count"] == 1
        assert len(data["spans"]) == 1

    def test_publish_metrics_best_effort(self):
        """publish_metrics doesn't raise even when Redis is unavailable."""
        ctx = TraceContext(run_id="r1", agent_id="a1")
        with patch("redis.Redis", side_effect=Exception("no redis")):
            ctx.publish_metrics({"status": "completed", "duration_ms": 100})
        # No exception raised


class TestSpan:
    def test_duration_ms(self):
        s = Span(name="test", start_time=100.0, end_time=100.5)
        assert s.duration_ms == 500

    def test_duration_ms_zero(self):
        s = Span(name="test")
        assert s.duration_ms == 0

    def test_to_dict(self):
        s = Span(name="x", span_id="abc", status="ok")
        d = s.to_dict()
        assert d["name"] == "x"
        assert d["span_id"] == "abc"
        assert d["status"] == "ok"
