"""Explicit fixture-only routing preference; never broadens a provider allowlist."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def apply(model: Any, kwargs: Any, preference: Any) -> Any:
    if preference not in {"latency", "throughput"}:
        raise ValueError("Unsupported experimental provider sort")
    if not model.startswith("openrouter/"):
        return kwargs
    result = deepcopy(kwargs)
    extra = result.setdefault("extra_body", {})
    provider = extra.setdefault("provider", {})
    if provider.get("order") or provider.get("sort"):
        raise ValueError("Experiment must not override an existing provider preference")
    provider["sort"] = preference
    return result
