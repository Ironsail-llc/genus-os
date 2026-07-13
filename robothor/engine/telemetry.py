"""
Structured Telemetry — OpenTelemetry-compatible trace/span IDs.

Generates trace and span IDs without requiring an OTel collector dependency.
Spans nest (LLM calls contain child tool call spans). Serialized spans are
stored as metadata on the run. Metrics published to Redis for dashboards.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


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
    _span_stack: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Generator[Span, None, None]:
        """Context manager for a timed span."""
        parent_id = self._span_stack[-1].span_id if self._span_stack else None
        s = Span(
            name=name,
            parent_span_id=parent_id,
            start_time=time.time(),
            attributes=attributes,
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
        """Publish run metrics to Redis event bus. Best-effort."""
        try:
            import redis

            r = redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
            )
            r.xadd(
                "robothor:events:telemetry",
                {
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
                },
                maxlen=5000,
            )
        except Exception as e:
            logger.debug("Failed to publish telemetry to Redis: %s", e)

        # Optionally export to OTLP collector
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            self._export_otlp(otlp_endpoint, run_data)

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
        return {"service.name": "robothor-engine", "agent.id": self.agent_id}

    def _span_to_otlp(self, s: Span) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "spanId": s.span_id,
            "parentSpanId": s.parent_span_id or "",
            "name": s.name,
            "startTimeUnixNano": str(int(s.start_time * 1e9)) if s.start_time else "0",
            "endTimeUnixNano": str(int(s.end_time * 1e9)) if s.end_time else "0",
            "attributes": _otlp_attrs(s.attributes),
            "status": {"code": 2 if s.status == "error" else 1},
        }

    def _export_otlp(self, endpoint: str, run_data: dict[str, Any]) -> None:
        """Export trace data to an OTLP HTTP endpoint. Best-effort."""
        try:
            import httpx

            url = f"{endpoint.rstrip('/')}/v1/traces"
            httpx.post(url, json=self.build_otlp_payload(run_data), timeout=5.0)
        except Exception as e:
            logger.debug("Failed to export OTLP trace: %s", e)
