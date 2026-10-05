"""Every tool this instance has CLASSIFIED as having no side effects.

Three sources, one answer. ``READONLY_TOOLS`` is core's own table — the one
plan mode has relied on since it existed — and the other two arrive at
runtime: an MCP adapter's ``read_only:`` list and a plugin's ``read_only``
entry in its ``genus.tools`` payload. Both are validated at load (a list of
names drawn from what the bundle actually provides, or the bundle is refused),
so nothing here has to re-check them.

Absent means WRITE, in both seams. That direction is the whole point: a bundle
that declares nothing contributes nothing, so a capability nobody classified
can never be treated as safe by the controls that read this — plan mode, the
benchmark allow-list, and the parallel-execution planner.

This module exists because the third of those was about to be a fourth copy of
the union. The adapter half lived as a private helper inside
``tools/handlers/benchmark.py``, which is the wrong home for it the moment a
second caller appears: a second copy of a safety classification is the drift
that `hardcoded-names-drift` records, and here the drift would decide whether
two tool calls run at the same time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import READONLY_TOOLS

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "adapter_read_only_tools",
    "declared_read_only_tools",
    "is_read_only_call",
    "plugin_read_only_tools",
]


def adapter_read_only_tools() -> frozenset[str]:
    """Adapter tools their own bundle declared read-only.

    Adapter tools are registered dynamically from an MCP server's
    ``tools/list``, so they never appear in the static schema registry and
    cannot be enumerated at import time. Until 2026-09-10 one operator's four
    adapter tool names were hardcoded into core's benchmark allow-list, which
    shipped a stranger's vendor and silently denied every other instance's
    adapters.

    Never raises: an instance with no adapters, or a loader that cannot answer,
    contributes no names — which is the safe direction.
    """
    try:
        from robothor.engine.adapters import get_loaded_adapters

        names: set[str] = set()
        for adapter in get_loaded_adapters():
            names.update(adapter.read_only)
        return frozenset(names)
    except Exception as exc:  # noqa: BLE001 - "no adapters" is not a failure
        logger.debug("adapter read-only classification unavailable: %s", exc)
        return frozenset()


def plugin_read_only_tools() -> frozenset[str]:
    """Plugin tools their own package declared read-only.

    Same seam, same rule, same fail-closed behaviour as the adapter half. A
    plugin that fails to load contributes nothing rather than stopping the
    caller — one broken package must not be able to make the engine refuse to
    classify anything.
    """
    try:
        from robothor.plugins import load_plugins

        return frozenset(load_plugins(reserved_names=set()).read_only)
    except Exception as exc:  # noqa: BLE001 - one broken package is not an outage
        logger.debug("plugin read-only classification unavailable: %s", exc)
        return frozenset()


def declared_read_only_tools() -> frozenset[str]:
    """Core's table plus whatever the installed bundles declared.

    A function, not a constant: the second and third sources depend on what
    this process loaded at runtime, and a module-level union would freeze the
    answer at import time — before any adapter has connected.
    """
    return frozenset(READONLY_TOOLS | adapter_read_only_tools() | plugin_read_only_tools())


#: The deferred-toolset meta-tool: it has no effect of its own, only the one of
#: the tool it runs.
_WRAPPER_TOOL = "tool_call"


def _read_only_by_arguments(name: str, arguments: dict[str, Any]) -> bool:
    """Tools that are a read in one argument shape and a write in another.

    ``pr_review_intake(count_only=True)`` only counts the queue; without it
    (or with ``pr=``) it polls, files tasks and starts reviews.
    """
    if name == "pr_review_intake":
        return arguments.get("count_only") is True and not arguments.get("pr")
    return False


def is_read_only_call(
    name: str, arguments: Any = None, read_only: Iterable[str] | None = None
) -> bool:
    """Whether THIS call — a name with its arguments — has no side effects.

    ``tool_call`` inherits the classification of the call it wraps, so a
    deferred agent reading through the wrapper is treated exactly as one that
    reads directly. ``read_only`` defaults to core's own table; callers that
    honour bundle declarations pass :func:`declared_read_only_tools`.
    """
    names = READONLY_TOOLS if read_only is None else frozenset(read_only)
    args = arguments if isinstance(arguments, dict) else {}
    if name == _WRAPPER_TOOL:
        inner = args.get("name")
        if not isinstance(inner, str) or not inner.strip() or inner.strip() == _WRAPPER_TOOL:
            return False
        return is_read_only_call(inner.strip(), args.get("arguments"), names)
    return name in names or _read_only_by_arguments(name, args)
