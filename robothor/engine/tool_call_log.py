"""One structured log line per tool call.

Every tool call passes through ``ToolRegistry.execute``; :func:`logged` wraps it
and writes one line on the ``robothor.engine.tool_calls`` logger::

    event=tool_call tool=<name> agent=<id> run_id=<id> outcome=<outcome>
        error_type=<type> duration_ms=<n> reason="<short redacted text>"

``workflow=<id>`` takes the place of ``agent=`` for a workflow step's call.
``outcome`` is one of :data:`OUTCOMES`: ``ok``; ``error`` (the tool failed, or
another system refused it); ``denied`` (a role or allowlist gate refused it);
``refused`` (any other engine gate, or a redirection such as the repeat
guard); ``timeout``; ``cancelled``. ``ok`` logs at INFO, every other outcome
at WARNING. Tool arguments and result bodies are never logged; ``reason`` is
the error text only, through :func:`robothor.engine.log_fields.short_reason`.

A ``tool_call`` wrapper is logged as the tool it wraps, once: when the wrapped
tool runs, its own line is the line; when the wrapper refuses before it runs,
the wrapper's line names the wrapped tool. A call refused at admission, before
it reaches the registry, is logged by :func:`log_tool_call` from the refusal
recorder.

Logging never fails the call: if writing the line raises, one ERROR goes to
this module's own logger and the result is returned as it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import time
from typing import TYPE_CHECKING, Any, TypeVar, cast

from robothor.engine.log_fields import kv_line, short_reason

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["OUTCOMES", "classify_result", "log_tool_call", "logged"]

_F = TypeVar("_F", bound="Callable[..., Awaitable[Any]]")

logger = logging.getLogger(__name__)
line_logger = logging.getLogger("robothor.engine.tool_calls")

OUTCOMES = frozenset({"ok", "error", "refused", "denied", "timeout", "cancelled"})
_DENIED_GATES = frozenset({"role", "allowlist"})
_WORKFLOW_PREFIX = "workflow:"

#: Set by a ``tool_call`` wrapper for the one wrapped call it runs.
_wrapped: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "robothor_tool_call_wrapped", default=None
)


def classify_result(tool_name: str, result: Any) -> tuple[str, str, str]:
    """``(outcome, error_type, raw_reason)`` of a tool result."""
    if not isinstance(result, dict):
        return "ok", "", ""
    from robothor.engine.tool_outcome import error_text

    message = error_text(result) or ""
    if result.get("refused") is True:
        gate = str(result.get("gate") or "")
        name = str(result.get("gate_name") or "")
        error_type = f"{gate}.{name}" if gate and name else gate or "redirect"
        outcome = "denied" if gate in _DENIED_GATES else "refused"
        return outcome, error_type, message or str(result.get("reason") or "")
    if not message:
        return "ok", "", ""
    if message.startswith(f"Tool '{tool_name}' timed out after"):
        return "timeout", "timeout", message
    error_type = str(result.get("exception") or result.get("error_type") or "")
    if not error_type:
        from robothor.engine.error_recovery import classify_error

        error_type = classify_error(tool_name, message[:4096]).value
    return "error", error_type, message


def log_tool_call(
    *,
    tool: str,
    agent_id: str,
    run_id: str,
    outcome: str,
    error_type: str = "",
    reason: str = "",
    duration_ms: int = 0,
) -> None:
    """Write the line. Never raises."""
    try:
        who: dict[str, Any]
        if agent_id.startswith(_WORKFLOW_PREFIX):
            who = {"workflow": agent_id[len(_WORKFLOW_PREFIX) :]}
        else:
            who = {"agent": agent_id}
        fields: dict[str, Any] = {"tool": tool, **who, "run_id": run_id, "outcome": outcome}
        if outcome != "ok":
            fields["error_type"] = error_type
        fields["duration_ms"] = duration_ms
        if outcome != "ok":
            fields["reason"] = short_reason(reason)
        level = logging.INFO if outcome == "ok" else logging.WARNING
        line_logger.log(level, "%s", kv_line("tool_call", **fields))
    except Exception:  # noqa: BLE001 — a log line must never fail the call
        with contextlib.suppress(Exception):
            logger.error("tool_call log line failed for %s", tool, exc_info=True)


def logged(execute: _F) -> _F:
    """Wrap ``ToolRegistry.execute`` so every call writes one line."""
    from robothor.engine.wrapped_call import WRAPPER_TOOL, inner_call

    @functools.wraps(execute)
    async def _execute(self: Any, tool_name: str, arguments: Any, *args: Any, **kwargs: Any) -> Any:
        holder = _wrapped.get()
        if holder is not None and not holder["ran"] and holder["tool"] == tool_name:
            holder["ran"] = True
        own: dict[str, Any] | None = None
        if tool_name == WRAPPER_TOOL:
            own = {"tool": inner_call(tool_name, arguments)[0], "ran": False}
        token = _wrapped.set(own)
        started = time.monotonic()
        outcome, error_type, reason = "ok", "", ""
        try:
            result = await execute(self, tool_name, arguments, *args, **kwargs)
            try:
                outcome, error_type, reason = classify_result(tool_name, result)
            except Exception:  # noqa: BLE001 — classifying never fails the call
                outcome, error_type = "unknown", "unclassified"
            return result
        except asyncio.CancelledError:
            outcome, error_type = "cancelled", "cancelled"
            raise
        except TimeoutError as exc:
            outcome, error_type, reason = "timeout", type(exc).__name__, str(exc)
            raise
        except Exception as exc:
            outcome, error_type, reason = "error", type(exc).__name__, str(exc)
            raise
        finally:
            _wrapped.reset(token)
            if own is None or not own["ran"]:
                log_tool_call(
                    tool=own["tool"] if own is not None else tool_name,
                    agent_id=str(kwargs.get("agent_id") or ""),
                    run_id=str(kwargs.get("run_id") or ""),
                    outcome=outcome,
                    error_type=error_type,
                    reason=reason,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

    return cast("_F", _execute)
