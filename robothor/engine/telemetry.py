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
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


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
    spans: list[Span] = field(default_factory=list)
    _span_stack: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Generator[Span, None, None]:
        """Context manager for a timed span."""
        parent_id = self._span_stack[-1].span_id if self._span_stack else None
        s = Span(
            name=name,
            parent_span_id=parent_id,
            start_time=time.monotonic(),
            attributes=attributes,
        )
        self._span_stack.append(s)
        try:
            yield s
        except Exception:
            s.status = "error"
            raise
        finally:
            s.end_time = time.monotonic()
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
                },
                maxlen=5000,
            )
        except Exception as e:
            logger.debug("Failed to publish telemetry: %s", e)
