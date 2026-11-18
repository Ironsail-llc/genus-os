"""One structured log line per tool call.

Every tool call ends in exactly one INFO line on this module's logger::

    event=tool_call tool=<name> agent=<id> run_id=<id> outcome=<ok|error|refused|deferred>
        duration_ms=<int> reason="<code>" [error_class=<ExceptionName>]

A workflow ``tool`` step carries ``workflow=<id>`` in place of ``agent``, and
the workflow run's id as ``run_id``. The same fields ride on the record as
``extra`` attributes. Field meanings are documented in ``docs/OBSERVABILITY.md``.

The arguments and the result are never logged, not even redacted: either can
hold anything a person typed. Nor is any message text from a tool or a service
it called: ``reason`` is a fixed code from :data:`REASONS`, ``error_class`` is
an exception's class name only, and ``tool`` is written only when it names a
registered tool (otherwise ``unknown``).

Written from three places, which between them see every call exactly once:
``ToolRegistry.execute`` (:func:`begin` / :func:`finish`: every call that
reaches dispatch, from an agent turn, a workflow step, ``execute_code`` or
``tool_call``), admission (:func:`log_admission`: a call a gate refused before
dispatch), and the two refusals that precede both (a workflow step's guardrail
block, and :func:`log_snippet_refusal` for the ``execute_code`` caps). The
outcome of a dispatched call comes from the status its audit row records
(:func:`note_audit_status`), the codes dispatch notes (:func:`note_failure`)
and the result's structural markers (``refused``,
``guard``, ``outcome_unknown``, ``would_have_done``), never from message text.
Writing the line can never fail the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "FIELDS",
    "OUTCOMES",
    "REASONS",
    "WOULD_HAVE_DONE_REASON",
    "begin",
    "emit",
    "finish",
    "guardrail_reason",
    "log_admission",
    "log_snippet_refusal",
    "note_audit_status",
    "note_failure",
    "workflow_run_scope",
]

#: Every outcome a line can carry.
OUTCOMES = ("ok", "error", "refused", "deferred")

#: The fields a line carries (``agent`` or ``workflow``; ``error_class`` only
#: when an exception was caught).
FIELDS = (
    "event",
    "tool",
    "agent",
    "workflow",
    "run_id",
    "outcome",
    "reason",
    "error_class",
    "duration_ms",
)

#: ``reason`` for a write the tool recorded instead of making, because no
#: write connection is configured (a result carrying ``would_have_done``).
WOULD_HAVE_DONE_REASON = "no write connection: would have done"

#: The closed set ``reason`` is drawn from. Engine-defined codes only: nothing
#: a tool, a model or a remote service wrote can reach the line through it.
#: Anything else is written as ``other``.
REASONS = (
    "",
    "timeout",
    "cancelled",
    "permission_denied",
    "approval_refused",
    "not_allowed",
    "plan_mode",
    "wrap_up",
    "hook",
    "guardrail",
    "rate_limited",
    "policy",
    "disabled",
    "runtime_denied",
    "repeat_guard",
    "sandbox",
    "snippet_cap",
    "unknown_tool",
    "validation",
    "backing_service",
    "tool_error",
    "pending",
    "other",
    WOULD_HAVE_DONE_REASON,
)
_REASON_SET = frozenset(REASONS)

_WORKFLOW_PREFIX = "workflow:"
_UNKNOWN_TOOL = "unknown"
_CLASS_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

#: A refusal's ``gate`` / ``gate_name`` (``robothor.engine.refusal``) -> reason.
_GATE_NAMES = {"rbac": "permission_denied", "per_task_whitelist": "not_allowed"}
_GATES = {
    "role": "permission_denied",
    "allowlist": "not_allowed",
    "runtime": "runtime_denied",
    "flag": "disabled",
    "policy": "policy",
}
#: A result's or verdict's ``guard`` marker -> reason.
_GUARDS = {
    "plan_mode": "plan_mode",
    "tools_allowed": "not_allowed",
    "run_wrapup": "wrap_up",
    "is_benchmark": "sandbox",
    "execute_code_call_cap": "snippet_cap",
    "execute_code_approval_cap": "snippet_cap",
}

#: The workflow run a workflow ``tool`` step belongs to. Its tool calls run
#: under ``agent_id="workflow:<id>"`` with no agent run id of their own.
_workflow_run: ContextVar[str | None] = ContextVar("tool_call_log_workflow_run", default=None)


@dataclass
class _Call:
    tool: str
    agent_id: str
    run_id: str
    arguments: Any
    started: float
    wrapper: bool = False
    parent: _Call | None = None
    audit_status: str = ""
    reason: str = ""
    error_class: str = ""
    inner_logged: bool = False


_current: ContextVar[_Call | None] = ContextVar("tool_call_log_current", default=None)


@contextmanager
def workflow_run_scope(run_id: str) -> Iterator[None]:
    """Name the workflow run for the tool calls made inside the block."""
    token = _workflow_run.set(run_id)
    try:
        yield
    finally:
        _workflow_run.reset(token)


def begin(tool: str, arguments: Any, *, agent_id: str, run_id: str) -> tuple[_Call, Token[Any]]:
    """Start the record for one call reaching the registry."""
    from robothor.engine.wrapped_call import WRAPPER_TOOL

    call = _Call(
        tool=tool,
        agent_id=agent_id,
        run_id=run_id,
        arguments=arguments,
        started=time.monotonic(),
        wrapper=tool == WRAPPER_TOOL,
        parent=_current.get(),
    )
    return call, _current.set(call)


def note_audit_status(status: str) -> None:
    """What the audit row said about the call in flight (``ok``, ``error``, ``denied``),
    or ``timeout`` from the registry's own deadline."""
    call = _current.get()
    if call is not None:
        call.audit_status = status


def note_failure(reason: str, exc: BaseException | None = None) -> None:
    """Why the call in flight failed, as a code from :data:`REASONS`, and the
    class of the exception behind it. Never its message."""
    call = _current.get()
    if call is not None:
        call.reason = reason
        call.error_class = type(exc).__name__ if exc is not None else ""


def finish(
    call: _Call, token: Token[Any], result: Any = None, exc: BaseException | None = None
) -> None:
    """Close the record and write its one line. Never raises."""
    with contextlib.suppress(Exception):  # a token from another context
        _current.reset(token)
    try:
        if call.parent is not None and call.parent.wrapper:
            if isinstance(exc, asyncio.CancelledError):
                # The wrapper's own deadline (or the run) cancelled this call:
                # the wrapper sees why and writes the one line, as a timeout
                # when its deadline fired, under this call's name.
                return
            call.parent.inner_logged = True
        if call.wrapper and call.inner_logged:
            return  # the wrapped tool's own call already wrote the line
        tool = _judged_name(call.tool, call.arguments) if call.wrapper else call.tool
        outcome, reason = _classify(result, exc, call)
        error_class = type(exc).__name__ if exc is not None else call.error_class
        emit(
            tool,
            agent_id=call.agent_id,
            run_id=call.run_id,
            outcome=outcome,
            reason=reason,
            error_class=error_class,
            duration_ms=int((time.monotonic() - call.started) * 1000),
        )
    except Exception as e:  # noqa: BLE001 - logging never fails a call
        _report_failure(e)


def emit(
    tool: str,
    *,
    agent_id: str,
    run_id: str,
    outcome: str,
    reason: str = "",
    error_class: str = "",
    duration_ms: int = 0,
) -> None:
    """Write one ``event=tool_call`` line. Never raises.

    ``tool`` is written only when it names a registered tool, ``reason`` only
    when it is in :data:`REASONS` and ``error_class`` only when it is an
    identifier, so no free text reaches the line through any of them.
    """
    try:
        from robothor.sanitize import sanitize_log

        if agent_id.startswith(_WORKFLOW_PREFIX):
            who, caller = "workflow", agent_id[len(_WORKFLOW_PREFIX) :]
            run_id = run_id or (_workflow_run.get() or "")
        else:
            who, caller = "agent", agent_id or "unknown"
        fields: dict[str, Any] = {
            "event": "tool_call",
            "tool": _loggable_name(tool),
            who: sanitize_log(caller),
            "run_id": sanitize_log(run_id),
            "outcome": outcome if outcome in OUTCOMES else "error",
            "reason": reason if reason in _REASON_SET else "other",
            "duration_ms": int(duration_ms),
        }
        message = "event=tool_call tool=%s %s=%s run_id=%s outcome=%s duration_ms=%d reason=%s"
        args: list[Any] = [
            fields["tool"],
            who,
            fields[who],
            fields["run_id"],
            fields["outcome"],
            fields["duration_ms"],
            json.dumps(fields["reason"]),
        ]
        if error_class and _CLASS_NAME.match(error_class):
            fields["error_class"] = error_class
            message += " error_class=%s"
            args.append(error_class)
        logger.info(message, *args, extra=fields)
    except Exception as e:  # noqa: BLE001 - logging never fails a call
        _report_failure(e)


async def log_admission(gates: Awaitable[Any], kw: dict[str, Any]) -> Any:
    """Await admission's verdict; write the line for a call it refused.

    A refused call never reaches the registry, so its line is written here,
    under the name of the call the gates judged (the wrapped one, for
    ``tool_call``), with the admission time as its duration.
    """
    started = time.monotonic()
    verdict = await gates
    if not getattr(verdict, "allowed", True):
        emit(
            _judged_name(kw.get("tool_name", ""), kw.get("tool_args")),
            agent_id=str(getattr(kw.get("agent_config"), "id", "") or ""),
            run_id=_run_id(kw.get("session")),
            outcome="refused",
            reason=_verdict_reason(getattr(verdict, "output", None) or {}),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return verdict


def log_snippet_refusal(
    req: Any, name: str, args: dict[str, Any], refusal: dict[str, Any]
) -> dict[str, Any]:
    """Write the line for a proxied call an ``execute_code`` cap refused; return the refusal."""
    emit(
        _judged_name(name, args),
        agent_id=str(getattr(getattr(req, "agent_config", None), "id", "") or ""),
        run_id=_run_id(getattr(req, "session", None)),
        outcome="refused",
        reason="snippet_cap",
    )
    return refusal


def guardrail_reason(guardrail_name: str) -> str:
    """The reason code for a guardrail that blocked a call."""
    if guardrail_name == "rbac":
        return "permission_denied"
    return "rate_limited" if guardrail_name == "rate_limit" else "guardrail"


def _verdict_reason(output: dict[str, Any]) -> str:
    """An admission refusal's reason, from the marker its gate recorded.

    The two human-approval denials are the only refusals that record none.
    """
    if output.get("guard"):
        return _GUARDS.get(str(output["guard"]), "policy")
    if output.get("hook"):
        return "hook"
    if output.get("guardrail"):
        return guardrail_reason(str(output["guardrail"]))
    return "approval_refused"


def _judged_name(tool: str, arguments: Any) -> str:
    """The tool a call runs: for ``tool_call``, the one it wraps."""
    try:
        from robothor.engine.wrapped_call import inner_call

        return inner_call(tool, arguments or {})[0] or tool
    except Exception:  # noqa: BLE001 - a malformed wrapper is logged as itself
        return tool


def _loggable_name(tool: str) -> str:
    """``tool`` if it names a registered tool, else ``unknown``.

    A name the model typed for a tool that does not exist, or put inside a
    ``tool_call``, is free text like any other argument.
    """
    with contextlib.suppress(Exception):  # an unreadable registry names nothing
        from robothor.engine.tools import get_registry
        from robothor.engine.tools.dispatch import _get_handlers

        if tool in _get_handlers() or get_registry().get_adapter_route(tool):
            return tool
    return _UNKNOWN_TOOL


def _run_id(session: Any) -> str:
    return str(getattr(getattr(session, "run", None), "id", "") or "")


def _report_failure(e: Exception) -> None:
    with contextlib.suppress(Exception):
        logger.error("tool_call line could not be written: %s", type(e).__name__)


def _classify(result: Any, exc: BaseException | None, call: _Call) -> tuple[str, str]:
    """(outcome, reason code) from structure: the exception's type, the audit
    status, the codes dispatch noted, and the result's markers. Never text."""
    if exc is not None:
        if isinstance(exc, TimeoutError):
            return "error", "timeout"
        return "error", "cancelled" if isinstance(exc, asyncio.CancelledError) else "tool_error"
    if not isinstance(result, dict):
        return "ok", ""
    failed = bool(result.get("error"))
    if result.get("refused") is True:
        return "refused", _refusal_reason(result)
    if result.get("repeat_guard") and (failed or call.audit_status == "denied"):
        return "refused", "repeat_guard"
    if failed and result.get("guard"):
        return "refused", _GUARDS.get(str(result["guard"]), "policy")
    if call.audit_status == "denied":
        return "refused", "runtime_denied"
    if result.get("outcome_unknown") is True:
        return "deferred", "pending"  # an effect awaiting reconciliation
    if call.audit_status == "timeout":
        return "error", "timeout"
    if failed:
        return "error", call.reason or "tool_error"
    if result.get("would_have_done") is True:
        return "ok", WOULD_HAVE_DONE_REASON
    return "ok", ""


def _refusal_reason(result: dict[str, Any]) -> str:
    gate_name = str(result.get("gate_name", ""))
    if gate_name in _GATE_NAMES:
        return _GATE_NAMES[gate_name]
    return _GATES.get(str(result.get("gate", "")), "policy")
