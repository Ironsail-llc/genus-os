"""Per-run compaction state shared by the runner and provider preflight."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextControl:
    active_request: str = ""
    fingerprint: str = ""
    model: str = ""
    threshold: int = 0
    stalled_tokens: int = 0
    measurements: list[dict[str, Any]] = field(default_factory=list)

    def skip(self, messages: list[dict[str, Any]], model: str, threshold: int, tokens: int) -> bool:
        if model != self.model or threshold != self.threshold:
            return False
        if self.fingerprint == fingerprint(messages):
            return True
        # A protected prefix that cannot fit must not cause another summary
        # on every tool result. A materially larger context gets another try.
        return bool(self.stalled_tokens and tokens < self.stalled_tokens * 1.2)


def fingerprint(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()


control: ContextVar[ContextControl | None] = ContextVar("context_control", default=None)
