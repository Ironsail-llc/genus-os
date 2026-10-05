"""What a ``tool_call`` really is: the call it wraps.

A deferred agent (more tools than ``ROBOTHOR_DEFERRED_TOOLS_THRESHOLD``)
reaches its opt-in tools through the ``tool_call`` meta-tool, which has no
budget and no effect of its own. Every control that keys on a tool's NAME
therefore has to look through the wrapper, or it judges the wrapper instead
of the work. Observed 2026-10-05, both ways at once: ``tool_call(name=
"claude_code_wait", arguments={"timeout_s": 1200})`` was cut at the 120 s
default (the deadline was resolved for ``tool_call``), and the cancelled read
was then journalled as an uncertain write that fenced every later wait.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["WRAPPER_TOOL", "inner_call", "is_read_only_call"]

WRAPPER_TOOL = "tool_call"


def inner_call(name: str, arguments: Any = None) -> tuple[str, dict[str, Any]]:
    """``(name, arguments)`` of the call that actually runs.

    For ``tool_call`` that is the wrapped tool. Anything that is not a plain
    tool name — missing, not a string, or another meta-tool the handler will
    refuse — is left as the wrapper itself.
    """
    args = arguments if isinstance(arguments, dict) else {}
    if name != WRAPPER_TOOL:
        return name, args
    inner = args.get("name")
    if not isinstance(inner, str) or not inner.strip() or inner.strip() == WRAPPER_TOOL:
        return name, args
    wrapped = args.get("arguments")
    return inner.strip(), wrapped if isinstance(wrapped, dict) else {}


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

    ``tool_call`` inherits the classification of the call it wraps.
    ``read_only`` defaults to core's own table (``READONLY_TOOLS``).
    """
    if read_only is None:
        from robothor.engine.tools.constants import READONLY_TOOLS

        read_only = READONLY_TOOLS
    name, args = inner_call(name, arguments)
    if name == WRAPPER_TOOL:
        return False
    return name in frozenset(read_only) or _read_only_by_arguments(name, args)
