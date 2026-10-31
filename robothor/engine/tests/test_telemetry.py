"""Tests for structured telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine import telemetry
from robothor.engine.task_registry import get_task_registry, reset_task_registry
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
            with ctx.span("failing"):
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


# ── Publishing off the event loop ─────────────────────────────────────

_RUN_DATA: dict[str, Any] = {
    "status": "completed",
    "duration_ms": 1234,
    "input_tokens": 500,
    "output_tokens": 200,
    "cache_creation_tokens": 100,
    "cache_read_tokens": 300,
    "cache_hit_ratio": 0.6,
}

_EXPECTED_FIELDS = {
    "type",
    "agent_id",
    "run_id",
    "trace_id",
    "span_count",
    "duration_ms",
    "status",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cache_hit_ratio",
}

_SLOW_SECONDS = 2.0
_MAX_LOOP_GAP = 0.5  # the old blocking path stalls for the full _SLOW_SECONDS


def _closed_port() -> int:
    """A loopback port nothing listens on (connections are refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def _isolated_publisher(monkeypatch: pytest.MonkeyPatch):
    """Fresh publisher state, no real Redis, no collector or OTel env unless a test sets one."""
    monkeypatch.setattr(telemetry, "_publisher", None, raising=False)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(_closed_port()))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    reset_task_registry()
    yield
    reset_task_registry()


@pytest.fixture
def async_redis():
    """A fake ``redis.asyncio`` module whose client records awaited xadd calls."""
    client = MagicMock()
    client.xadd = AsyncMock(return_value="1-0")
    client.aclose = AsyncMock()
    aio = MagicMock()
    aio.Redis.return_value = client
    root = MagicMock()
    root.asyncio = aio
    with patch.dict("sys.modules", {"redis": root, "redis.asyncio": aio}):
        yield aio, client


class _Collector:
    """A slow OTLP/HTTP collector on loopback, run in its own thread."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.bodies: list[dict[str, Any]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                collector.bodies.append({"path": self.path, "json": json.loads(body)})
                time.sleep(collector.delay)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> _Collector:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


class _SlowRedis:
    """Accepts a connection, holds it for ``delay`` seconds, then closes it."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = int(self.sock.getsockname()[1])
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._hold, args=(conn,), daemon=True).start()

    def _hold(self, conn: socket.socket) -> None:
        time.sleep(self.delay)
        conn.close()

    def __enter__(self) -> _SlowRedis:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self.thread.join(timeout=1)
        self.sock.close()


async def _max_loop_gap(duration: float) -> float:
    """Tick every 10 ms for ``duration`` seconds; return the longest gap seen."""
    worst = 0.0
    last = time.perf_counter()
    end = last + duration
    while time.perf_counter() < end:
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        worst = max(worst, now - last)
        last = now
    return worst


def _trace(n: int = 1) -> TraceContext:
    trace = TraceContext(agent_id="example-agent", run_id=f"run-{n}")
    with trace.span("llm_call", model="example/model"):
        pass
    return trace


async def _publish_and_watch(trace: TraceContext) -> tuple[float, float]:
    """Publish inside the loop while a ticker runs; return (call seconds, worst gap)."""
    ticker = asyncio.create_task(_max_loop_gap(_SLOW_SECONDS + 0.3))
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    trace.publish_metrics(dict(_RUN_DATA))
    elapsed = time.perf_counter() - t0
    gap = await ticker
    await get_task_registry().drain(timeout=5.0)
    return elapsed, gap


class TestPublishOffTheLoop:
    async def test_slow_collector_does_not_block_the_loop(self, monkeypatch, async_redis):
        with _Collector(_SLOW_SECONDS) as collector:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.endpoint + "/")
            trace = _trace()
            elapsed, gap = await _publish_and_watch(trace)

        assert elapsed < 0.05, f"publish_metrics held the loop for {elapsed:.3f}s"
        assert gap < _MAX_LOOP_GAP, f"event loop stalled for {gap:.3f}s"
        assert len(collector.bodies) == 1
        assert collector.bodies[0]["path"] == "/v1/traces"
        assert collector.bodies[0]["json"] == trace.build_otlp_payload()

    async def test_slow_redis_does_not_block_the_loop(self, monkeypatch):
        with _SlowRedis(_SLOW_SECONDS) as fake:
            monkeypatch.setenv("REDIS_PORT", str(fake.port))
            elapsed, gap = await _publish_and_watch(_trace())

        assert elapsed < 0.05, f"publish_metrics held the loop for {elapsed:.3f}s"
        assert gap < _MAX_LOOP_GAP, f"event loop stalled for {gap:.3f}s"

    async def test_xadd_wire_format_unchanged(self, async_redis):
        aio, client = async_redis
        trace = _trace()
        trace.publish_metrics(dict(_RUN_DATA))
        await get_task_registry().drain(timeout=2.0)

        client.xadd.assert_awaited_once()
        args, kwargs = client.xadd.call_args
        assert args[0] == "robothor:events:telemetry"
        fields = args[1]
        assert set(fields) == _EXPECTED_FIELDS
        assert fields["type"] == "agent.run.completed"
        assert fields["agent_id"] == "example-agent"
        assert fields["run_id"] == "run-1"
        assert fields["trace_id"] == trace.trace_id
        assert fields["span_count"] == "1"
        assert fields["duration_ms"] == "1234"
        assert fields["status"] == "completed"
        assert fields["input_tokens"] == "500"
        assert fields["output_tokens"] == "200"
        assert fields["cache_creation_tokens"] == "100"
        assert fields["cache_read_tokens"] == "300"
        assert fields["cache_hit_ratio"] == "0.6"
        assert kwargs == {"maxlen": 5000}
        _, client_kwargs = aio.Redis.call_args
        assert client_kwargs["decode_responses"] is True

    async def test_one_shared_client_per_sink(self, monkeypatch, async_redis):
        aio, client = async_redis
        with (
            _Collector(0.0) as collector,
            patch("httpx.AsyncClient", wraps=httpx.AsyncClient) as http_client,
        ):
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.endpoint)
            for n in range(5):
                _trace(n).publish_metrics(dict(_RUN_DATA))
                await get_task_registry().drain(timeout=2.0)

        assert aio.Redis.call_count == 1
        assert http_client.call_count == 1
        assert client.xadd.await_count == 5

    async def test_spans_queued_during_a_slow_post_are_batched(self, monkeypatch, async_redis):
        with _Collector(0.5) as collector:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.endpoint)
            traces = [_trace(n) for n in range(5)]
            for trace in traces:
                trace.publish_metrics(dict(_RUN_DATA))
                await asyncio.sleep(0)
            await get_task_registry().drain(timeout=5.0)

        assert len(collector.bodies) < 5
        sent = [rs for body in collector.bodies for rs in body["json"]["resourceSpans"]]
        expected = [rs for t in traces for rs in t.build_otlp_payload()["resourceSpans"]]
        assert sent == expected

    async def test_failures_logged_once_and_never_raised(self, monkeypatch, async_redis, caplog):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{_closed_port()}")
        with caplog.at_level(logging.DEBUG, logger="robothor.engine.telemetry"):
            for n in range(3):
                _trace(n).publish_metrics(dict(_RUN_DATA))
                await get_task_registry().drain(timeout=5.0)

        warnings = [
            r
            for r in caplog.records
            if r.name == "robothor.engine.telemetry"
            and r.levelno == logging.WARNING
            and "OTLP" in r.getMessage()
        ]
        assert len(warnings) == 1
        background_errors = [r for r in caplog.records if r.name == "robothor.engine.task_registry"]
        assert background_errors == []


# ── OTLP resource attributes from the standard OTel env ──────────────


def _resource(trace: TraceContext) -> dict[str, Any]:
    attrs = trace.build_otlp_payload()["resourceSpans"][0]["resource"]["attributes"]
    return {a["key"]: a["value"]["stringValue"] for a in attrs}


class TestOtlpResourceAttributes:
    def test_defaults_without_env(self):
        assert _resource(_trace()) == {
            "service.name": "robothor-engine",
            "agent.id": "example-agent",
        }

    def test_resource_attributes_env_is_merged(self, monkeypatch):
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "deployment.environment=staging, service.namespace=example",
        )
        assert _resource(_trace()) == {
            "service.name": "robothor-engine",
            "agent.id": "example-agent",
            "deployment.environment": "staging",
            "service.namespace": "example",
        }

    def test_env_wins_over_defaults_and_service_name_wins_over_both(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=from-attrs,team=a%20b")
        assert _resource(_trace())["service.name"] == "from-attrs"
        assert _resource(_trace())["team"] == "a b"
        monkeypatch.setenv("OTEL_SERVICE_NAME", "example-service")
        assert _resource(_trace())["service.name"] == "example-service"

    def test_malformed_entries_are_skipped(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "novalue,=empty,,ok=1, spaced = yes ")
        res = _resource(_trace())
        assert res["ok"] == "1"
        assert res["spaced"] == "yes"
        assert "novalue" not in res
        assert "" not in res
