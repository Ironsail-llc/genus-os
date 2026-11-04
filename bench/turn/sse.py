"""Parse the engine's webchat SSE stream into events and per-turn timings.

``/chat/send`` (robothor/engine/chat.py) answers with ``event:``/``data:``
frames: ``interim``, ``delta`` (one per streamed chunk), tool and status
events, then ``done`` (or ``error``). The HTTP half records each line's
arrival time, so the timings here are what a browser would see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass
class SseEvent:
    event: str
    data: Any
    t: float  # arrival time of the frame's last line, seconds


@dataclass
class TurnTiming:
    ttft_ms: float | None = None
    done_ms: float | None = None
    finalize_tail_ms: float | None = None
    run_id: str | None = None
    status: str | None = None
    error: str | None = None
    deltas: int = 0
    done_data: dict[str, Any] = field(default_factory=dict)


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_sse(lines: Iterable[tuple[float, str]]) -> list[SseEvent]:
    """Group ``(arrival_time, line)`` pairs into events; comments are dropped."""
    events: list[SseEvent] = []
    name = ""
    data: list[str] = []
    last_t = 0.0

    def flush() -> None:
        nonlocal name, data
        if name or data:
            events.append(
                SseEvent(event=name or "message", data=_decode("\n".join(data)), t=last_t)
            )
        name, data = "", []

    for t, line in lines:
        line = line.rstrip("\r\n")
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value.removeprefix(" ")
        last_t = t
        if key == "event":
            name = value
        elif key == "data":
            data.append(value)
    flush()
    return events


def turn_timings(events: list[SseEvent], t0: float) -> TurnTiming:
    """Milliseconds from the request (``t0``) to first delta and to ``done``."""
    timing = TurnTiming()
    deltas = [e for e in events if e.event == "delta"]
    timing.deltas = len(deltas)
    if deltas:
        timing.ttft_ms = (deltas[0].t - t0) * 1000.0
    for event in events:
        if event.event == "done":
            timing.done_ms = (event.t - t0) * 1000.0
            data = event.data if isinstance(event.data, dict) else {}
            timing.done_data = data
            timing.run_id = data.get("run_id")
            timing.status = data.get("status")
            if deltas:
                timing.finalize_tail_ms = (event.t - deltas[-1].t) * 1000.0
        elif event.event == "error":
            data = event.data if isinstance(event.data, dict) else {"error": event.data}
            timing.error = str(data.get("error", data))
    return timing
