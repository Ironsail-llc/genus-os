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

import json
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import TOOLSEARCH_TOOLS

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "PERMISSION_TRANSPARENT_TOOLS",
    "WRAPPER_TOOL",
    "MalformedToolCallError",
    "explicit_arguments",
    "inner_call",
    "is_read_only_call",
    "unwrap",
    "wrapped_arguments",
]

WRAPPER_TOOL = "tool_call"

#: The meta-tools a deferred run reaches its tools through. They carry no
#: permission of their own: ``tool_search`` and ``tool_describe`` only read the
#: run's allow-set, and RBAC on a ``tool_call`` is decided by the tool it wraps,
#: which the handler runs through the same dispatch and the same permission
#: check a direct call meets. Requiring a ``role_permissions`` row for them made
#: every deferred tool unreachable to a role whose rows never name them.
PERMISSION_TRANSPARENT_TOOLS = TOOLSEARCH_TOOLS

_HOW_TO_SEND = (
    "send tool_call with name set to the tool to run and arguments as an object "
    "of its parameters; tool_describe(name=...) gives the schema"
)


class MalformedToolCallError(ValueError):
    """A ``tool_call`` the model built wrongly: a bad call it can fix, not a crash."""


def explicit_arguments(raw: Any) -> dict[str, Any] | None:
    """The arguments the model actually sent as an object, or ``None``.

    An object is itself; a JSON string that parses to an object is the same
    call. Anything else, a null or an empty string included, is not an object:
    readers that audit what a tool was asked to do (receipts, verification)
    must not take the step's output as evidence of a call with no arguments.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def wrapped_arguments(raw: Any) -> dict[str, Any] | None:
    """The wrapped call's arguments for dispatch: an object, or ``None`` if they
    are not one.

    Missing (or empty) arguments mean none, so a tool with no parameters runs;
    otherwise as :func:`explicit_arguments`.
    """
    if raw is None or raw == "":
        return {}
    return explicit_arguments(raw)


def unwrap(args: Any) -> tuple[str, dict[str, Any]]:
    """``(name, arguments)`` of the call a ``tool_call`` wraps.

    Raises :class:`MalformedToolCallError` naming what is wrong with the wrapper
    and how to send it, so admission and the handler refuse it the same way.
    """
    args = args if isinstance(args, dict) else {}
    inner = args.get("name")
    if not isinstance(inner, str) or not inner.strip():
        raise MalformedToolCallError(
            f"tool_call needs name: the tool to run, as a string; {_HOW_TO_SEND}"
        )
    inner = inner.strip()
    if inner == WRAPPER_TOOL:
        raise MalformedToolCallError(f"tool_call cannot wrap another tool_call; {_HOW_TO_SEND}")
    arguments = wrapped_arguments(args.get("arguments"))
    if arguments is None:
        raise MalformedToolCallError(
            f"tool_call arguments for {inner!r} must be an object (or a JSON string of one), "
            f"not {type(args.get('arguments')).__name__}; {_HOW_TO_SEND}"
        )
    return inner, arguments


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
    wrapped = wrapped_arguments(args.get("arguments"))
    return inner.strip(), wrapped if wrapped is not None else {}


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
