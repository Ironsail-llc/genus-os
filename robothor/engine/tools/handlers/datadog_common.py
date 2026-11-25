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


def masked(value: Any) -> Any:
    """``value`` with every email address and phone number in its strings masked."""
    if isinstance(value, str):
        return redact_personal_data(value)
    if isinstance(value, dict):
        return {k: masked(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [masked(v) for v in value]
    return value


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
    check: Callable[[DatadogScope], object],
    build: Callable[[], AsyncDatadogClient],
    unlock: str | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """``call`` under this tenant's scope: (its value, None), or (None, the answer).

    The scope comes from ``ctx.tenant_id`` and nowhere else. ``check`` judges
    the caller's input against the scope (raising :class:`ScopeError`) before
    ``build`` makes the client, so an escape is refused even on an instance
    with no key, and nothing is sent for a refusal. ``build`` blocks (the
    vault read) and runs off the loop. ``unlock`` is the refusal's advice
    when the default (balance the brackets) does not fit the input.
    """
    scope = await asyncio.to_thread(scope_for, ctx.tenant_id)
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
