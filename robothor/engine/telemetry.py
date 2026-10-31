"""
Structured Telemetry — OpenTelemetry-compatible trace/span IDs.

Generates trace and span IDs without requiring an OTel collector dependency.
Spans nest (LLM calls contain child tool call spans). Serialized spans are
stored as metadata on the run. Metrics published to Redis for dashboards.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

TELEMETRY_STREAM = "robothor:events:telemetry"
TELEMETRY_STREAM_MAXLEN = 5000
OTLP_TIMEOUT_S = 5.0
REDIS_TIMEOUT_S = 5.0
OTLP_BATCH_MAX = 64
ROOT_SPAN_NAME = "agent_run"
# OTLP SpanKind enum values.
_OTLP_SPAN_KIND = {"internal": 1, "server": 2, "client": 3}


def _otlp_value(v: Any) -> dict[str, Any]:
    """Convert a Python value to an OTLP AnyValue."""
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_otlp_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _otlp_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an attribute dict to OTLP KeyValue list."""
    return [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()]


def cache_hit_ratio(cache_read_tokens: int, prompt_tokens: int) -> float:
    """Fraction of prompt tokens served from the prompt cache, in [0.0, 1.0].

    ``prompt_tokens`` is floored at 1 so a zero-token run (or a run that
    hasn't accumulated any input tokens yet) never divides by zero, and the
    result is clamped to 1.0 so degenerate accounting (cache_read reported
    higher than prompt tokens) stays a well-formed ratio for dashboards.
    """
    return min((cache_read_tokens or 0) / max(prompt_tokens or 0, 1), 1.0)


def gen_ai_attributes(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    finish_reason: str = "",
    system: str = "",
) -> dict[str, Any]:
    """Build OpenTelemetry GenAI semantic-convention attributes for an LLM span."""
    attrs: dict[str, Any] = {
        "gen_ai.system": system or _gen_ai_system(model),
        "gen_ai.request.model": model,
    }
    if input_tokens:
        attrs["gen_ai.usage.input_tokens"] = int(input_tokens)
    if output_tokens:
        attrs["gen_ai.usage.output_tokens"] = int(output_tokens)
    if cache_read_tokens:
        attrs["gen_ai.usage.cache_read_input_tokens"] = int(cache_read_tokens)
    if cache_creation_tokens:
        attrs["gen_ai.usage.cache_creation_input_tokens"] = int(cache_creation_tokens)
    if cache_read_tokens or cache_creation_tokens:
        attrs["gen_ai.usage.cache_hit_ratio"] = cache_hit_ratio(cache_read_tokens, input_tokens)
    if finish_reason:
        attrs["gen_ai.response.finish_reasons"] = [finish_reason]
    return attrs


def _gen_ai_system(model: str) -> str:
    m = (model or "").lower()
    if "anthropic" in m or "claude" in m:
        return "anthropic"
    if "openai" in m or "gpt" in m or "codex" in m:
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    return m.split("/")[0] if "/" in m else "unknown"


def _trace_id() -> str:
    """Generate a 32-char hex trace ID (OTel compatible)."""
    return uuid.uuid4().hex


def _span_id() -> str:
    """Generate a 16-char hex span ID (OTel compatible)."""
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    """A single span in a trace."""

    name: str
    span_id: str = field(default_factory=_span_id)
    parent_span_id: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok, error
    status_message: str = ""
    kind: str = "internal"  # internal, server, client (OTel SpanKind)

    @property
    def duration_ms(self) -> int:
        if self.end_time and self.start_time:
            return int((self.end_time - self.start_time) * 1000)
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "status": self.status,
        }


@dataclass
class TraceContext:
    """Manages a single trace for an agent run."""

    trace_id: str = field(default_factory=_trace_id)
    run_id: str = ""
    agent_id: str = ""
    # Sub-agent trace linkage — reuse parent trace_id for unified traces
    parent_trace_id: str = ""
    parent_span_id: str = ""
    spans: list[Span] = field(default_factory=list)
    # Set once the run's metrics have been published (one publish per trace).
    published: bool = False
    # The run's root span (``agent_run``), open from start_run() to end_run().
    _root: Span | None = None
    _span_stack: list[Span] = field(default_factory=list)

    def start_run(self, start_time: float | None = None) -> None:
        """Open the run's root span; every span opened after it is its child."""
        if self._root is not None:
            return
        self._root = Span(
            name=ROOT_SPAN_NAME,
            parent_span_id=self.parent_span_id or None,
            start_time=start_time or time.time(),
            kind="server",
        )
        self._span_stack.insert(0, self._root)

    def end_run(self, *, status: str, trigger_type: str, error: str = "") -> None:
        """Close the run's root span at run exit. Idempotent; no-op without start_run()."""
        root = self._root
        if root is None or root.end_time:
            return
        root.end_time = time.time()
        root.attributes["run.status"] = status
        root.attributes["run.trigger_type"] = trigger_type
        if error:
            root.status = "error"
            root.status_message = error
            root.attributes["error.message"] = error
        if root in self._span_stack:
            self._span_stack.remove(root)
        self.spans.append(root)

    @contextmanager
    def span(
        self, name: str, *, span_kind: str = "internal", **attributes: Any
    ) -> Generator[Span, None, None]:
        """Context manager for a timed span."""
        parent_id = self._span_stack[-1].span_id if self._span_stack else None
        s = Span(
            name=name,
            parent_span_id=parent_id,
            start_time=time.time(),
            attributes=attributes,
            kind=span_kind,
        )
        self._span_stack.append(s)
        try:
            yield s
        except Exception:
            s.status = "error"
            raise
        finally:
            s.end_time = time.time()
            self._span_stack.pop()
            self.spans.append(s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }

    def publish_metrics(self, run_data: dict[str, Any]) -> None:
        """Publish run metrics to Redis and, if configured, OTLP. Best-effort.

        Inside a running event loop this never blocks it: the stream fields and
        the OTLP payload are built here, and the network writes run as tracked
        background tasks on shared async clients (see ``_TelemetryPublisher``).
        From a worker thread it hands off to the engine loop's publisher; with no
        engine loop at all (CLI) it publishes synchronously.
        """
        try:
            fields = self._stream_fields(run_data)
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            payload = self.build_otlp_payload(run_data) if endpoint else None
            if loop is not None:
                _get_publisher(loop).submit(fields, endpoint, payload)
                return
            # A worker thread (a finalizer under asyncio.to_thread): hand the
            # write to the engine loop's publisher when there is one.
            publisher = _publisher
            if publisher is not None and publisher.loop.is_running():
                publisher.loop.call_soon_threadsafe(publisher.submit, fields, endpoint, payload)
                return
            self._publish_sync(fields, endpoint, run_data)
        except Exception as e:
            logger.debug("Failed to publish telemetry: %s", e)

    def _stream_fields(self, run_data: dict[str, Any]) -> dict[str, str]:
        """The ``robothor:events:telemetry`` entry for this run."""
        return {
            "type": "agent.run.completed",
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_count": str(len(self.spans)),
            "duration_ms": str(run_data.get("duration_ms", 0)),
            "status": run_data.get("status", ""),
            "input_tokens": str(run_data.get("input_tokens", 0)),
            "output_tokens": str(run_data.get("output_tokens", 0)),
            "cache_creation_tokens": str(run_data.get("cache_creation_tokens", 0)),
            "cache_read_tokens": str(run_data.get("cache_read_tokens", 0)),
            "cache_hit_ratio": str(run_data.get("cache_hit_ratio", 0)),
        }

    def _publish_sync(
        self, fields: dict[Any, Any], endpoint: str | None, run_data: dict[str, Any]
    ) -> None:
        """Synchronous publish, for callers with no running event loop."""
        try:
            import redis

            host, port = _redis_address()
            r = redis.Redis(host=host, port=port, decode_responses=True)
            r.xadd(TELEMETRY_STREAM, fields, maxlen=TELEMETRY_STREAM_MAXLEN)
        except Exception as e:
            logger.debug("Failed to publish telemetry to Redis: %s", e)

        # Optionally export to OTLP collector
        if endpoint:
            self._export_otlp(endpoint, run_data)

    def build_otlp_payload(self, run_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a full OTLP/HTTP traces payload — span attributes, durations, and
        status, not just status (the prior export dropped attributes/durations).
        """
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": _otlp_attrs(self._resource_attributes())},
                    "scopeSpans": [
                        {
                            "scope": {"name": "robothor.engine"},
                            "spans": [self._span_to_otlp(s) for s in self.spans],
                        }
                    ],
                }
            ]
        }

    def _resource_attributes(self) -> dict[str, Any]:
        """Engine defaults, overridden by the standard OTel resource env vars.

        ``OTEL_RESOURCE_ATTRIBUTES`` (comma-separated ``key=value``, values
        percent-decoded) wins over the defaults, and ``OTEL_SERVICE_NAME``
        wins over ``service.name`` from either, as the OTel spec orders them.
        """
        attrs: dict[str, Any] = {"service.name": "robothor-engine", "agent.id": self.agent_id}
        for entry in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
            key, sep, value = entry.partition("=")
            key = key.strip()
            if sep and key:
                attrs[key] = unquote(value.strip())
        service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip()
        if service_name:
            attrs["service.name"] = service_name
        return attrs

    def _span_to_otlp(self, s: Span) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "spanId": s.span_id,
            "parentSpanId": s.parent_span_id or "",
            "name": s.name,
            "startTimeUnixNano": str(int(s.start_time * 1e9)) if s.start_time else "0",
            "endTimeUnixNano": str(int(s.end_time * 1e9)) if s.end_time else "0",
            "attributes": _otlp_attrs(s.attributes),
            "status": _otlp_status(s),
            "kind": _OTLP_SPAN_KIND.get(s.kind, _OTLP_SPAN_KIND["internal"]),
        }

    def _export_otlp(self, endpoint: str, run_data: dict[str, Any]) -> None:
        """Export trace data to an OTLP HTTP endpoint. Best-effort."""
        try:
            import httpx

            httpx.post(
                _otlp_url(endpoint), json=self.build_otlp_payload(run_data), timeout=OTLP_TIMEOUT_S
            )
        except Exception as e:
            logger.debug("Failed to export OTLP trace: %s", e)


def _redis_address() -> tuple[str, int]:
    """Where run telemetry is written (``REDIS_HOST`` / ``REDIS_PORT``)."""
    return os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379"))


def _otlp_status(s: Span) -> dict[str, Any]:
    status: dict[str, Any] = {"code": 2 if s.status == "error" else 1}
    if s.status == "error" and s.status_message:
        status["message"] = s.status_message
    return status


def _otlp_url(endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/v1/traces"


def _merge_otlp(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """One OTLP/HTTP request body carrying every payload's resourceSpans."""
    if len(payloads) == 1:
        return payloads[0]
    return {"resourceSpans": [rs for p in payloads for rs in p["resourceSpans"]]}


class _TelemetryPublisher:
    """Shared async telemetry sinks for one event loop.

    One ``redis.asyncio`` client and one ``httpx.AsyncClient``, created on
    first use. Each run's XADD is its own background task; OTLP payloads go on
    a queue drained by a single exporter task, so payloads queued while
    a POST is in flight leave together in the next one. Every task is spawned
    under the engine's TaskRegistry (drained on shutdown). A failing sink logs
    one WARNING, then DEBUG; nothing here ever reaches the run.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._redis: Any = None
        self._http: Any = None
        self._otlp_queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._exporter: asyncio.Task[Any] | None = None
        self._warned: set[str] = set()

    def submit(
        self, fields: dict[str, str], endpoint: str | None, payload: dict[str, Any] | None
    ) -> None:
        from robothor.engine.task_registry import get_task_registry

        registry = get_task_registry()
        registry.spawn(self._xadd(fields), name="telemetry:redis")
        if not endpoint or payload is None:
            return
        self._otlp_queue.append((endpoint, payload))
        if self._exporter is None or self._exporter.done():
            self._exporter = registry.spawn(self._drain_otlp(), name="telemetry:otlp")

    async def _xadd(self, fields: dict[str, str]) -> None:
        try:
            if self._redis is None:
                aioredis = await _import_off_loop("redis.asyncio")
                host, port = _redis_address()
                self._redis = aioredis.Redis(
                    host=host,
                    port=port,
                    decode_responses=True,
                    socket_connect_timeout=REDIS_TIMEOUT_S,
                    socket_timeout=REDIS_TIMEOUT_S,
                )
            await self._redis.xadd(TELEMETRY_STREAM, fields, maxlen=TELEMETRY_STREAM_MAXLEN)
        except Exception as e:
            self._fail("redis", "Failed to publish telemetry to Redis: %s", e)

    async def _drain_otlp(self) -> None:
        while self._otlp_queue:
            endpoint = self._otlp_queue[0][0]
            batch: list[dict[str, Any]] = []
            while (
                self._otlp_queue
                and len(batch) < OTLP_BATCH_MAX
                and self._otlp_queue[0][0] == endpoint
            ):
                batch.append(self._otlp_queue.popleft()[1])
            try:
                if self._http is None:
                    httpx = await _import_off_loop("httpx")
                    # The first client loads the TLS trust store (~0.3 s).
                    self._http = await asyncio.to_thread(httpx.AsyncClient, timeout=OTLP_TIMEOUT_S)
                await self._http.post(_otlp_url(endpoint), json=_merge_otlp(batch))
            except Exception as e:
                self._fail("otlp", "Failed to export OTLP trace: %s", e)

    def _fail(self, sink: str, msg: str, *args: Any) -> None:
        level = logging.DEBUG if sink in self._warned else logging.WARNING
        self._warned.add(sink)
        logger.log(level, msg, *args)


async def _import_off_loop(name: str) -> Any:
    """Import a module in a worker thread: a cold ``import redis`` takes ~0.3 s."""
    module = sys.modules.get(name)
    if module is not None:
        return module
    return await asyncio.to_thread(importlib.import_module, name)


_publisher: _TelemetryPublisher | None = None


def _get_publisher(loop: asyncio.AbstractEventLoop) -> _TelemetryPublisher:
    """The publisher bound to ``loop`` (async clients cannot cross loops)."""
    global _publisher
    if _publisher is None or _publisher.loop is not loop:
        _publisher = _TelemetryPublisher(loop)
    return _publisher
