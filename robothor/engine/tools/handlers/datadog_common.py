"""What every Datadog tool shares: the one scoped-call path, refusals, the audit row.

Not a handler module (it registers no tool). The log tools
(:mod:`.datadog_logs`) and the span, trace, monitor and metric tools
(:mod:`.datadog_more`) both go through :func:`scoped`, so there is one place
where the scope is taken from ``ctx.tenant_id``, judged before any client is
built, and turned into the standard refusal when it does not hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.integrations.datadog.client import DatadogError
from robothor.integrations.datadog.scope import ScopeError, scope_for
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.datadog.client import AsyncDatadogClient
    from robothor.integrations.datadog.scope import DatadogScope

logger = logging.getLogger(__name__)

#: How much serialized result a call hands the model. Fitting, not a limit on
#: use: a result that does not fit is cut and says so (``truncated``).
RESULT_BUDGET_CHARS = 20_000
#: Room kept for the envelope around the rows: the note, the cursor, the window.
ENVELOPE_CHARS = 1000
#: How much of the caller's query the audit row keeps.
_AUDIT_QUERY_CHARS = 300

_QUERY_UNLOCK = "Rewrite the query so its brackets, parentheses and quotes balance, then retry."


# ── refusals and errors ────────────────────────────────────────────────────


def not_configured() -> dict[str, Any]:
    from robothor.engine.refusal import refusal

    return refusal(
        "Datadog is not configured for this organization.",
        gate="runtime",
        name="datadog_scope",
        unlock=(
            "The operator adds this tenant to ROBOTHOR_DATADOG_SCOPES "
            "(docs/OBSERVABILITY.md, Reading Datadog from Genus)."
        ),
    )


def query_refused(reason: str, unlock: str = _QUERY_UNLOCK) -> dict[str, Any]:
    from robothor.engine.refusal import refusal

    return refusal(
        f"The Datadog query was refused: {reason}.",
        gate="policy",
        name="datadog_query",
        unlock=unlock,
    )


def failed(tool: str, exc: DatadogError) -> dict[str, Any]:
    """The kind is the whole explanation: never a URL, a key or a vendor body."""
    return {
        "error": f"{tool} failed: Datadog answered {exc.kind}.",
        "kind": exc.kind,
        "retryable": exc.kind in ("rate_limit", "network"),
    }


# ── the audit row ──────────────────────────────────────────────────────────


def audit(
    event: str, tool: str, ctx: ToolContext, query: str, details: dict[str, Any], status: str
) -> None:
    """One ``event`` row (``datadog.logs_read`` and its siblings), queued off the loop.

    Never raises. Its own event rather than extra fields on
    ``agent.tool_call``: that row deliberately carries no arguments, and there
    is no hook for a handler to add any. Under ``ctx.tenant_id``, the tenant
    whose data was read; a cross-tenant call also leaves
    ``agent.cross_tenant_call`` under the home tenant.
    """
    try:
        from robothor.audit.logger import log_event
        from robothor.engine.event_writer import get_event_writer
        from robothor.secrets.redaction import redact

        shown = redact_personal_data(redact(query[:4096]))[:_AUDIT_QUERY_CHARS]
        get_event_writer().submit(
            log_event,
            event_type=event,
            action=tool,
            category="agent",
            actor=ctx.agent_id or "unknown",
            user_id=ctx.user_id,
            details={"tool": tool, "query": shown, "run_id": ctx.run_id, **details},
            status=status,
            tenant_id=ctx.tenant_id,
        )
    except Exception:  # noqa: BLE001 - audit must not break the read it records
        logger.warning("datadog audit row could not be queued", exc_info=True)


# ── arguments and shaping ──────────────────────────────────────────────────


def text(args: dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def count(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


#: Attribute keys whose value is a phone number whatever its shape: a number
#: stored as an integer, or as bare digits, matches no phone pattern.
_PHONE_KEY = re.compile(
    r"phone|mobile|msisdn|(?:^|[_.-])(?:tel|fax|cell)(?:$|[_.-])", re.IGNORECASE
)
_DIGITS_IN_A_PHONE = 7


def masked(value: Any, key: str = "") -> Any:
    """``value`` with email addresses and phone numbers masked, at any depth."""
    if _PHONE_KEY.search(key) and (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, str) and sum(c.isdigit() for c in value) >= _DIGITS_IN_A_PHONE)
    ):
        return "<phone>"
    if isinstance(value, str):
        return redact_personal_data(value)
    if isinstance(value, dict):
        return {k: masked(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [masked(v, key) for v in value]
    return value


# ── the window ─────────────────────────────────────────────────────────────

#: Datadog's relative times: ``now``, or ``now-<n><unit>``.
_RELATIVE = re.compile(r"now(?:-(\d+)([smhdw]))?")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def absolute(value: str, now: datetime) -> str:
    """``value`` as an ISO-8601 time when it is a relative time, else unchanged.

    A relative window moves between calls, so a cursor handed out with one
    would page through a different set than the page it came from. Resolved
    once here and returned in ``window``, the model passes the same absolute
    window back with the cursor.
    """
    match = _RELATIVE.fullmatch(value.strip().lower())
    if match is None:
        return value
    amount, unit = match.groups()
    moment = now - timedelta(**{_UNITS[unit]: int(amount)}) if unit else now
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_window(
    args: dict[str, Any], now: datetime, default_start: str, default_end: str
) -> dict[str, str]:
    """The call's ``{start, end}``, relative times resolved against one ``now``."""
    return {
        "start": absolute(text(args, "start", default_start), now),
        "end": absolute(text(args, "end", default_end), now),
    }


def size(payload: Any) -> int:
    return len(json.dumps(payload, default=str))


# ── the one scoped-call path ───────────────────────────────────────────────


async def scoped(
    tool: str,
    event: str,
    ctx: ToolContext,
    query: str,
    audited: dict[str, Any],
    call: Callable[[AsyncDatadogClient, DatadogScope], Awaitable[Any]],
    *,
    part: Callable[[DatadogScope], object],
    check: Callable[[DatadogScope], object],
    build: Callable[[], AsyncDatadogClient],
    unlock: str | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """``call`` under this tenant's scope: (its value, None), or (None, the answer).

    The scope comes from ``ctx.tenant_id`` and nowhere else. ``part`` judges
    the tenant's own part of the scope for this kind of data alone (raising
    :class:`ScopeError`): a part that grants no access is "not configured",
    whatever the caller sent, never a hint to rewrite the query. ``check`` judges
    the caller's input against the scope (raising :class:`ScopeError`) before
    ``build`` makes the client, so an escape is refused even on an instance
    with no key, and nothing is sent for a refusal. ``build`` blocks (the
    vault read) and runs off the loop. ``unlock`` is the refusal's advice
    when the default (balance the brackets) does not fit the input.
    """
    scope = await asyncio.to_thread(scope_for, ctx.tenant_id)
    try:
        if scope is not None:
            part(scope)
    except ScopeError:
        scope = None
    if scope is None:
        audit(event, tool, ctx, query, {**audited, "rows": 0}, "denied")
        return None, not_configured()
    try:
        check(scope)
        client = await asyncio.to_thread(build)
        return await call(client, scope), None
    except ScopeError as exc:
        audit(event, tool, ctx, query, {**audited, "rows": 0}, "denied")
        return None, query_refused(exc.reason, unlock or _QUERY_UNLOCK)
    except DatadogError as exc:
        audit(event, tool, ctx, query, {**audited, "rows": 0, "kind": exc.kind}, "error")
        return None, failed(tool, exc)
