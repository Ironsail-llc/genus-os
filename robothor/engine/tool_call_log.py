"""One structured log line per tool call.

Every tool call passes through ``ToolRegistry.execute``; :func:`logged` wraps it
and writes one line on the ``robothor.engine.tool_calls`` logger::

    event=tool_call tool=<name> agent=<id> run_id=<id> outcome=<outcome>
        error_type=<type> duration_ms=<n> reason="<short redacted text>"
        tenant_id=<id> trace_id=<hex>

``workflow=<id>`` takes the place of ``agent=`` for a workflow step's call.
``outcome`` is one of :data:`OUTCOMES`: ``ok``; ``error`` (the tool failed, or
another system refused it); ``denied`` (a role or allowlist gate refused it);
``refused`` (any other engine gate, or a redirection such as the repeat
guard); ``timeout``; ``cancelled``. ``ok`` logs at INFO, every other outcome
at WARNING. Tool arguments and result bodies are never logged; ``reason`` is
the error text only, through :func:`robothor.engine.log_fields.short_reason`.
``tenant_id`` and ``trace_id`` come from the ``tenant_id`` and ``trace_id`` the
call was made with, each left out when empty (a workflow step has no trace).

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
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, TypeVar, cast

from robothor.engine.log_fields import kv_line, scope_fields, short_reason
from robothor.engine.repeat_guard import canonical_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

__all__ = [
    "OUTCOMES",
    "classify_result",
    "log_refused",
    "log_tool_call",
    "logged",
    "logs_guard_refusals",
    "run_id_scope",
]

_F = TypeVar("_F", bound="Callable[..., Awaitable[Any]]")

logger = logging.getLogger(__name__)
line_logger = logging.getLogger("robothor.engine.tool_calls")

OUTCOMES = frozenset({"ok", "error", "refused", "denied", "timeout", "cancelled"})
_DENIED_GATES = frozenset({"role", "allowlist"})
_WORKFLOW_PREFIX = "workflow:"

#: The run a caller that passes no ``run_id`` belongs to (a workflow step).
_scoped_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "robothor_tool_call_run_id", default=""
)


@contextlib.contextmanager
def run_id_scope(run_id: str) -> Iterator[None]:
    """Name ``run_id`` on the lines of calls inside that pass none of their own."""
    token = _scoped_run_id.set(run_id or "")
    try:
        yield
    finally:
        _scoped_run_id.reset(token)


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
        status = result.get("status_code", result.get("status"))
        if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
            # A generic API tool hands back an HTTP failure as a result. Only
            # the code is logged, never the body.
            return "error", f"http_{status}", f"HTTP {status}"
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
    tenant_id: str = "",
    trace_id: str = "",
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
        fields.update(scope_fields(tenant_id, trace_id))
        level = logging.INFO if outcome == "ok" else logging.WARNING
        line_logger.log(level, "%s", kv_line("tool_call", **fields))
    except Exception:  # noqa: BLE001 — a log line must never fail the call
        with contextlib.suppress(Exception):
            logger.error("tool_call log line failed for %s", tool, exc_info=True)


def log_refused(tool_name: str, result: dict[str, Any], *, tool_args: Any, session: Any) -> None:
    """The line for a call refused before the registry: admission, or a caller's own cap.

    ``result`` is the refusal handed back; one with no ``refused`` key is
    logged as a refusal all the same. Never raises.
    """
    try:
        from robothor.engine.wrapped_call import inner_call

        tool, _ = inner_call(tool_name, tool_args)
        shaped = dict(result)
        shaped.setdefault("refused", True)
        if not shaped.get("gate") and shaped.get("guard"):
            shaped["gate"], shaped["gate_name"] = "policy", shaped["guard"]
        outcome, error_type, reason = classify_result(tool, shaped)
        run = getattr(session, "run", None)
        log_tool_call(
            tool=tool,
            agent_id=str(getattr(run, "agent_id", "") or ""),
            run_id=str(getattr(run, "id", "") or ""),
            outcome=outcome,
            error_type=error_type,
            reason=reason,
            tenant_id=str(getattr(run, "tenant_id", "") or ""),
            trace_id=str(getattr(session, "trace_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 — a log line must never fail the call
        with contextlib.suppress(Exception):
            logger.error("tool_call log line failed for %s", tool_name, exc_info=True)


def logs_guard_refusals(*guards: str) -> Callable[[_F], _F]:
    """Wrap a caller's own ``call(name, args)`` so its named refusals log.

    For refusals a caller makes before admission or the registry, such as the
    ``execute_code`` proxy's call and approval caps. Each is matched on its
    ``guard`` key and logged as ``outcome=refused error_type=policy.<guard>``.
    """
    names = frozenset(guards)

    def _wrap(call: _F) -> _F:
        @functools.wraps(call)
        async def _call(self: Any, name: str, args: Any) -> Any:
            result = await call(self, name, args)
            if isinstance(result, dict) and result.get("guard") in names:
                session = getattr(getattr(self, "_req", None), "session", None)
                log_refused(name, result, tool_args=args, session=session)
            return result

        return cast("_F", _call)

    return _wrap


#: Calls refused in a run: ``run_id -> {canonical call -> refusal text}``. An
#: identical call later in the same run is answered from here, not made again.
#: Bounded: the oldest runs are dropped first.
_refused_calls: OrderedDict[str, dict[str, str]] = OrderedDict()
_REFUSAL_RUNS, _REFUSALS_PER_RUN = 256, 128
#: Outcomes and error types that mean "refused", not "failed": a plain error or
#: a timeout may succeed on a retry, so it is never remembered.
_REFUSED_OUTCOMES = frozenset({"refused", "denied"})
_REFUSED_ERROR_TYPES = frozenset({"http_401", "http_403"})


def _repeat_of_refusal(run_id: str, key: str) -> dict[str, Any] | None:
    original = _refused_calls.get(run_id, {}).get(key) if run_id else None
    if original is None:
        return None
    return {
        "error": (
            "This exact call was already refused in this run: "
            f"{original} Do not retry it; tell the person what refused it and who can grant it."
        ),
        "refused": True,
        "repeat_of_refused_call": True,
    }


def _remember_refusal(run_id: str, key: str, outcome: str, error_type: str, reason: str) -> None:
    if not run_id or (outcome not in _REFUSED_OUTCOMES and error_type not in _REFUSED_ERROR_TYPES):
        return
    if error_type == "redirect":  # no gate: "change the call", a one-shot redirection
        return
    calls = _refused_calls.setdefault(run_id, {})
    _refused_calls.move_to_end(run_id)
    if len(calls) < _REFUSALS_PER_RUN:
        calls[key] = short_reason(reason) or error_type
    while len(_refused_calls) > _REFUSAL_RUNS:
        _refused_calls.popitem(last=False)


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
        run_id = str(kwargs.get("run_id") or _scoped_run_id.get())
        # A wrapper's inner call follows its unlock; a workflow step has its own retries.
        agent = str(kwargs.get("agent_id") or "")
        followed = holder is not None or agent.startswith(_WORKFLOW_PREFIX)
        key = "" if followed else canonical_key(tool_name, arguments)
        try:
            repeat = _repeat_of_refusal(run_id, key) if key else None
            if repeat is not None:
                outcome, error_type, reason = (
                    "refused",
                    "repeat_refused",
                    "repeat of a refused call",
                )
                return repeat
            result = await execute(self, tool_name, arguments, *args, **kwargs)
            try:
                outcome, error_type, reason = classify_result(tool_name, result)
                if key:
                    _remember_refusal(run_id, key, outcome, error_type, reason)
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
                    run_id=run_id,
                    outcome=outcome,
                    error_type=error_type,
                    reason=reason,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    tenant_id=str(kwargs.get("tenant_id") or ""),
                    trace_id=str(kwargs.get("trace_id") or ""),
                )

    return cast("_F", _execute)
