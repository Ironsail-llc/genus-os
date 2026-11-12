"""One structured log line per tool call.

Every tool call ends in exactly one INFO line on this module's logger::

    event=tool_call tool=<name> agent=<id> run_id=<id> outcome=<ok|error|refused|deferred>
        duration_ms=<int> reason="<short text>"

A workflow ``tool`` step carries ``workflow=<id>`` in place of ``agent``, and
the workflow run's id as ``run_id``. The same fields ride on the record as
``extra`` attributes. Field meanings are documented in ``docs/OBSERVABILITY.md``.

The arguments and the result are never logged, not even redacted: either can
hold anything a person typed. ``reason`` is the failure's text (an error class
and a short message), redacted, cut short, and with any argument value it
quotes replaced.

Written from three places, which between them see every call exactly once:
``ToolRegistry.execute`` (:func:`begin` / :func:`finish`: every call that
reaches dispatch, from an agent turn, a workflow step, ``execute_code`` or
``tool_call``), admission (:func:`log_admission`: a call a gate refused before
dispatch), and the two refusals that precede both (a workflow step's guardrail
block, and :func:`log_snippet_refusal` for the ``execute_code`` caps). The
outcome of a dispatched call comes from the status its audit row records
(:func:`note_audit_status`) and the result's structural markers (``refused``,
``guard``, ``outcome_unknown``, ``would_have_done``), never from message text.
Writing the line can never fail the call.
"""

from __future__ import annotations

import contextlib
import json
import logging
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
    "WOULD_HAVE_DONE_REASON",
    "begin",
    "emit",
    "finish",
    "log_admission",
    "log_snippet_refusal",
    "note_audit_status",
    "workflow_run_scope",
]

#: Every outcome a line can carry.
OUTCOMES = ("ok", "error", "refused", "deferred")

#: The fields every line carries (plus ``agent`` or ``workflow``).
FIELDS = ("event", "tool", "agent", "workflow", "run_id", "outcome", "reason", "duration_ms")

#: ``reason`` for a write the tool recorded instead of making, because no
#: write connection is configured (a result carrying ``would_have_done``).
WOULD_HAVE_DONE_REASON = "no write connection: would have done"

_REASON_MAX = 200
_MIN_SCRUB_LEN = 4
_WORKFLOW_PREFIX = "workflow:"

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


def finish(
    call: _Call, token: Token[Any], result: Any = None, exc: BaseException | None = None
) -> None:
    """Close the record and write its one line. Never raises."""
    with contextlib.suppress(Exception):  # a token from another context
        _current.reset(token)
    try:
        if call.parent is not None and call.parent.wrapper:
            call.parent.inner_logged = True
        if call.wrapper and call.inner_logged:
            return  # the wrapped tool's own call already wrote the line
        tool = _judged_name(call.tool, call.arguments) if call.wrapper else call.tool
        outcome, reason = _classify(result, exc, call.audit_status)
        emit(
            tool,
            agent_id=call.agent_id,
            run_id=call.run_id,
            outcome=outcome,
            reason=reason,
            duration_ms=int((time.monotonic() - call.started) * 1000),
            arguments=call.arguments,
        )
    except Exception as e:  # noqa: BLE001 - logging never fails a call
        _report_failure(call.tool, e)


def emit(
    tool: str,
    *,
    agent_id: str,
    run_id: str,
    outcome: str,
    reason: str = "",
    duration_ms: int = 0,
    arguments: Any = None,
) -> None:
    """Write one ``event=tool_call`` line. Never raises."""
    try:
        from robothor.sanitize import sanitize_log

        reason = _clean_reason(reason, arguments)
        if agent_id.startswith(_WORKFLOW_PREFIX):
            who, caller = "workflow", agent_id[len(_WORKFLOW_PREFIX) :]
            run_id = run_id or (_workflow_run.get() or "")
        else:
            who, caller = "agent", agent_id or "unknown"
        fields: dict[str, Any] = {
            "event": "tool_call",
            "tool": sanitize_log(tool),
            who: sanitize_log(caller),
            "run_id": sanitize_log(run_id),
            "outcome": outcome,
            "reason": reason,
            "duration_ms": int(duration_ms),
        }
        logger.info(
            "event=tool_call tool=%s %s=%s run_id=%s outcome=%s duration_ms=%d reason=%s",
            fields["tool"],
            who,
            fields[who],
            fields["run_id"],
            outcome,
            fields["duration_ms"],
            json.dumps(reason),
            extra=fields,
        )
    except Exception as e:  # noqa: BLE001 - logging never fails a call
        _report_failure(tool, e)


async def log_admission(gates: Awaitable[Any], kw: dict[str, Any]) -> Any:
    """Await admission's verdict; write the line for a call it refused.

    A refused call never reaches the registry, so its line is written here,
    under the name of the call the gates judged (the wrapped one, for
    ``tool_call``), with the admission time as its duration.
    """
    started = time.monotonic()
    verdict = await gates
    if not getattr(verdict, "allowed", True):
        tool_args = kw.get("tool_args")
        emit(
            _judged_name(kw.get("tool_name", ""), tool_args),
            agent_id=str(getattr(kw.get("agent_config"), "id", "") or ""),
            run_id=_run_id(kw.get("session")),
            outcome="refused",
            reason=str(getattr(verdict, "message", "") or ""),
            duration_ms=int((time.monotonic() - started) * 1000),
            arguments=tool_args,
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
        reason=str(refusal.get("error", "")),
        arguments=args,
    )
    return refusal


def _judged_name(tool: str, arguments: Any) -> str:
    """The tool a call runs: for ``tool_call``, the one it wraps."""
    try:
        from robothor.engine.wrapped_call import inner_call

        return inner_call(tool, arguments or {})[0] or tool
    except Exception:  # noqa: BLE001 - a malformed wrapper is logged as itself
        return tool


def _run_id(session: Any) -> str:
    return str(getattr(getattr(session, "run", None), "id", "") or "")


def _report_failure(tool: str, e: Exception) -> None:
    with contextlib.suppress(Exception):
        logger.error("tool_call line could not be written for %s: %s", tool, type(e).__name__)


def _classify(result: Any, exc: BaseException | None, audit_status: str) -> tuple[str, str]:
    if exc is not None:
        return "error", f"{type(exc).__name__}: {exc}"
    if not isinstance(result, dict):
        return "ok", ""
    from robothor.engine.tool_outcome import error_text

    error = error_text(result) or ""
    if audit_status == "denied" or result.get("refused") is True:
        return "refused", error
    if error and result.get("guard"):
        return "refused", error
    if result.get("outcome_unknown") is True:
        return "deferred", error  # an effect awaiting reconciliation
    if audit_status == "timeout":
        return "error", f"TimeoutError: {error}"
    if error:
        return "error", error
    if result.get("would_have_done") is True:
        return "ok", WOULD_HAVE_DONE_REASON
    return "ok", ""


def _clean_reason(reason: str, arguments: Any) -> str:
    if not reason:
        return ""
    from robothor.secrets.redaction import redact

    text = str(redact(str(reason)))
    for value in sorted(_string_values(arguments), key=len, reverse=True):
        text = text.replace(value, "[argument]")
    text = " ".join(text.split())
    return text[:_REASON_MAX]


def _string_values(value: Any, depth: int = 0) -> set[str]:
    """Every string in ``value`` long enough to be worth taking out of a reason."""
    if isinstance(value, str):
        return {value} if len(value) >= _MIN_SCRUB_LEN else set()
    if depth > 4:
        return set()
    if not isinstance(value, (dict, list, tuple)):
        return set()
    children = value.values() if isinstance(value, dict) else value
    return set().union(*(_string_values(v, depth + 1) for v in children))
