"""Model-specific provider allowlists owned by one agent dispatch scope."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass


def parse_provider_order(value):
    """Reject ambiguous routing rather than silently broadening a manifest."""
    if not isinstance(value, dict):
        raise ValueError("model.provider_order must map OpenRouter model paths to provider lists")
    for model, providers in value.items():
        if (
            not isinstance(model, str)
            or not model.startswith("openrouter/")
            or len(model.split("/")) < 3
            or not isinstance(providers, list)
            or not providers
            or any(not isinstance(p, str) or not p.strip() or p != p.strip() for p in providers)
        ):
            raise ValueError("Invalid model.provider_order entry")
    return deepcopy(value)


@dataclass
class _Routes:
    order: dict[str, list[str]]
    active: bool = True


_routes: ContextVar[_Routes | None] = ContextVar("agent_provider_order", default=None)


@contextmanager
def provider_order_scope(order):
    routes = _Routes(parse_provider_order(order))
    token = _routes.set(routes)
    try:
        yield
    finally:
        routes.active = False
        _routes.reset(token)


def apply_provider_order(model, kwargs):
    routes = _routes.get()
    providers = routes.order.get(model) if routes and routes.active else None
    if not providers:
        return
    extra = deepcopy(kwargs.get("extra_body") or {})
    existing = extra.get("provider") or {}
    # Compatibility constraints already set by the engine remain authoritative.
    for key in ("only", "order"):
        if existing.get(key) and any(
            not any(
                p.lower() == v.lower() or p.lower().startswith(v.lower() + "/")
                for v in existing[key]
            )
            for p in providers
        ):
            raise ValueError("model.provider_order conflicts with required provider compatibility")
    extra["provider"] = {
        **existing,
        "only": list(providers),
        "order": list(providers),
        "allow_fallbacks": False,
    }
    kwargs["extra_body"] = extra
