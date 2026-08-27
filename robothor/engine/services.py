"""Arbitrary named services contributed by installed packages.

Every other plugin group here names a KIND the platform already knows about
— a tool, a guardrail, a model, a job. That is a list, and a list is bounded
by whoever maintains it.

Measured from DeepSeek Harness's source on 2026-08-27: **143 distinct
`ctx.*` surfaces**, with `ctx.provide()` used 330 times. Their architecture
doc says "~9 kinds"; the code says service registration is the primitive and
the kinds are built on top of it. That is why "everything is a plugin" holds
for them, and why counting our named groups against their doc measured the
wrong thing. Nine groups do not catch that by becoming ten.

So this group names no kind. A package registers whatever it likes under a
name; core or another package looks it up. Breadth stops being a count.

What does not change is the containment the named groups already carry, and
which their architecture doc addresses for none of its surfaces: a reserved
name is refused rather than silently winning, the contract version is
negotiated, and a package that fails to import is reported instead of taking
the lookup down with it. Their shape, our guarantees.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Names core owns. A package registering one of these is claiming a seam the
#: engine resolves internally, which is a takeover rather than an extension —
#: the same rule the tool and model groups already apply.
#:
#: Refusal is all-or-nothing: a package declaring one reserved name loses
#: every service it registered, not just the offending one. That is the
#: loader's behaviour for all groups and it is the right way round — a
#: package reaching for `memory` has shown what it is willing to do, and
#: honouring the rest would reward the instinct. It is silent from the
#: caller's side, so it is pinned by a test rather than left to surprise.
_RESERVED: frozenset[str] = frozenset(
    {
        "memory",
        "scheduler",
        "runner",
        "session",
        "llm",
        "sandbox",
        "guardrails",
        "tools",
        "config",
        "db",
    }
)

_cache: dict[str, Any] = {}
_cache_generation: int = -1


def reserved_service_names() -> frozenset[str]:
    """Service names a package may not claim."""
    return _RESERVED


def _services() -> dict[str, Any]:
    """Everything installed packages provide, cached per plugin generation."""
    global _cache, _cache_generation
    try:
        from robothor.plugins import generation, load_plugins
    except Exception:  # noqa: BLE001 - plugins are optional
        return {}

    current = generation()
    if current == _cache_generation:
        return _cache

    try:
        loaded = load_plugins(reserved_names=set(_RESERVED))
        resolved = dict(loaded.services or {})
    except Exception as exc:  # noqa: BLE001 - a lookup must never raise
        logger.warning("Plugin services unavailable: %s", exc)
        resolved = {}

    _cache = resolved
    _cache_generation = current
    return _cache


def get_service(name: str) -> Any:
    """The service registered under `name`, or None.

    None rather than raising: a caller asking whether an optional capability
    is installed should not have to guard the question.
    """
    return _services().get(name)


def list_services() -> list[str]:
    """Every service name currently provided, sorted."""
    return sorted(_services())
