"""Explicit fixture-only routing preference; never broadens a provider allowlist."""

from copy import deepcopy


def apply(model, kwargs, preference):
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
