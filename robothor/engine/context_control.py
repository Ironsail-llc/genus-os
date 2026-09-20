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


def drain_history(messages: list[dict[str, Any]], model: str, target: int) -> list[dict[str, Any]]:
    """Drop old complete exchanges without truncating pinned or recent content."""
    from robothor.engine.compaction import _find_safe_split_index, _split_for_summary
    from robothor.engine.context import estimate_tokens

    if estimate_tokens(messages, model or None) <= target:
        return messages
    head, retained, tail = _split_for_summary(messages)
    # Keep the last user turn and everything following it, or the last complete
    # tool exchange when all user turns have already been pinned in the head.
    keep = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=-1)
    if keep < 0:
        keep = _find_safe_split_index(tail, max(0, len(tail) - 1))
    start = 0
    while (
        start < keep and estimate_tokens([*head, *retained, *tail[start:]], model or None) > target
    ):
        end = start + 1
        while end < keep and tail[end].get("role") == "tool":
            end += 1
        start = end
    return [*head, *retained, *tail[start:]] if start else messages
