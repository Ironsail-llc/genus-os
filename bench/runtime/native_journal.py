"""Durable live-sample boundaries; missing terminal events remain unresolved."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any


class NativeJournal:
    def __init__(self, output: Any) -> None:
        self.path = output.with_suffix(".events.jsonl")
        # Never mix a fresh run with an earlier interrupted run's evidence.
        with self.path.open("x"):
            pass

    def record(self, event: Any, model: Any, repetition: Any, **details: Any) -> None:
        row = {
            "event": event,
            "model": model,
            "repetition": repetition,
            "recorded_at": datetime.now(UTC).isoformat(),
            **details,
        }
        with self.path.open("a") as file:
            file.write(json.dumps(row) + "\n")
            file.flush()
            os.fsync(file.fileno())


def failed_sample(model: Any, index: Any, exc: Any, duration_ms: Any) -> Any:
    import asyncio

    return {
        "model": model,
        "repetition": index,
        "status": "interrupted"
        if isinstance(exc, asyncio.CancelledError)
        else "timeout"
        if isinstance(exc, TimeoutError)
        else "failed",
        "error_type": type(exc).__name__,
        "duration_ms": duration_ms,
        "model_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "engine_cost_usd": None,
    }
