"""Observe streamed provider completion separately from connection creation."""

from __future__ import annotations

import time
from typing import Any


def field(value: Any, name: Any, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def observe_chunk(chunk: Any, call: Any, elapsed: Any) -> None:
    usage = field(chunk, "usage")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = field(usage, name)
        if type(value) is int and value >= 0:
            call.setdefault("reported_usage", {})[name] = value
    reasoning = field(field(usage, "completion_tokens_details"), "reasoning_tokens")
    if type(reasoning) is int and reasoning >= 0:
        call.setdefault("reported_usage", {})["reasoning_tokens"] = reasoning
    for choice in field(chunk, "choices", []) or []:
        delta = field(choice, "delta")
        for key, names in (
            ("first_answer_ms", ("content", "tool_calls")),
            ("first_reasoning_ms", ("reasoning_content", "reasoning_details")),
        ):
            if any(field(delta, name) for name in names):
                call.setdefault(key, elapsed)


class ObservedStream:
    def __init__(self, stream: Any, call: Any, started: Any) -> None:
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.call = call
        self.started = started
        call["stream_completed"] = False

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self.iterator.__anext__()
            observe_chunk(chunk, self.call, (time.perf_counter() - self.started) * 1000)
            return chunk
        except StopAsyncIteration:
            self.call["stream_completed"] = True
            raise
        except BaseException as exc:
            self.call["stream_error_type"] = type(exc).__name__
            raise
        finally:
            self.call["stream_elapsed_ms"] = (time.perf_counter() - self.started) * 1000

    async def aclose(self) -> None:
        close = getattr(self.stream, "aclose", None)
        if close is not None:
            await close()
