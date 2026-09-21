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


def strip_engine_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop engine-private ``_``-prefixed message keys before a provider call.

    ``_pin`` (and any sibling bookkeeping) is ours: it exists so compaction can
    recognise a message the ENGINE marked, rather than trusting a string a model
    or a tool result could type for itself. OpenAI-compatible providers reject
    unrecognised message fields outright, so the key must be gone by the time
    the payload is built.

    Copy-on-write, and a no-op that returns the same list object when there is
    nothing to strip — the session keeps its own dicts and their keys.
    """
    if not any(k.startswith("_") for m in messages for k in m):
        return messages
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def drain_history(messages: list[dict[str, Any]], model: str, target: int) -> list[dict[str, Any]]:
    """Drop old complete exchanges without truncating pinned or recent content.

    Linear, and deliberately so. This used to re-estimate the whole surviving
    prefix on every drop iteration — O(n^2) token counting, measured at 2024ms
    on a real 121-message history, all of it on the event loop that also serves
    Telegram, the health API and the scheduler. ``estimate_tokens`` is additive
    in its default heuristic (``chars//4 + 400*tool_calls``), so the surviving
    cost is arithmetic: one estimate of the whole list, one estimate per DROP
    CANDIDATE, and subtraction from there. One pass over the content, not one
    pass per iteration.

    Callers must run this off the loop (``asyncio.to_thread``): with
    ``ROBOTHOR_REAL_TOKENIZER_ENABLED`` the estimator is ``litellm.token_counter``,
    a synchronous CPU-bound call.
    """
    from robothor.engine.compaction import _find_safe_split_index, _split_for_summary
    from robothor.engine.context import estimate_tokens

    remaining = estimate_tokens(messages, model or None)
    if remaining <= target:
        return messages
    head, retained, tail = _split_for_summary(messages)
    # Keep the last user turn and everything following it, or the last complete
    # tool exchange when all user turns have already been pinned in the head.
    keep = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=-1)
    if keep < 0:
        keep = _find_safe_split_index(tail, max(0, len(tail) - 1))
    if keep <= 0:
        return messages
    # Only the messages that may actually be dropped are priced individually.
    cost = [estimate_tokens([m], model or None) for m in tail[:keep]]
    start = 0
    while start < keep and remaining > target:
        end = start + 1
        while end < keep and tail[end].get("role") == "tool":
            end += 1
        remaining -= sum(cost[start:end])
        start = end
    return [*head, *retained, *tail[start:]] if start else messages
