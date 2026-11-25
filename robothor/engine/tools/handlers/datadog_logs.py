"""Datadog log tools: a tenant searches and counts its OWN logs, read-only.

Several tenants share one Datadog account and one read-only key. The wall
between them is the scope filter :mod:`robothor.integrations.datadog.scope`
composes into every query, and these handlers take that scope from
``ctx.tenant_id`` and from nothing else -- no argument names a scope. That is
what makes them safe on ``CROSS_TENANT_READS``: a parent run that passes
``tenant`` is swapped into the child's context by dispatch
(``cross_tenant.run_handler``) and so reads with the child's scope, and a run
that may not reach a tenant is refused before the handler runs.

Every message and string attribute is passed through
:func:`~robothor.secrets.redaction.redact_personal_data`. Each call writes one
``datadog.logs_read`` audit row with the caller's query (redacted, cut short),
the window and the row count; never a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from robothor.integrations.datadog.client import (
    AsyncDatadogClient,
    DatadogError,
    async_client_from_settings,
)
from robothor.integrations.datadog.scope import ScopeError, compose, scope_for
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.datadog.requests import LogRow

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

SEARCH_TOOL = "datadog_logs_search"
AGGREGATE_TOOL = "datadog_logs_aggregate"
AUDIT_EVENT = "datadog.logs_read"

DEFAULT_START = "now-1h"
DEFAULT_END = "now"
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_AGGREGATE_LIMIT = 25

#: How much serialized result a call hands the model. Fitting, not a limit on
#: use: a page that does not fit is cut at a row boundary and ``next_cursor``
#: continues from the first row left out.
RESULT_BUDGET_CHARS = 20_000
#: Successive message lengths tried when a page does not fit, longest first.
_MESSAGE_CAPS = (2000, 500, 200)
#: The tags every row lifts out of its tag list.
_ROW_TAGS = ("env", "version")
#: How much of the caller's query the audit row keeps.
_AUDIT_QUERY_CHARS = 300

#: Builds the client. Blocking (the vault read): always called through
#: ``asyncio.to_thread``. Tests replace it with a client over a fake transport.
_client_factory: Callable[[], AsyncDatadogClient] = async_client_from_settings


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


# ── refusals and errors ────────────────────────────────────────────────────


def _not_configured() -> dict[str, Any]:
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


def _query_refused(reason: str) -> dict[str, Any]:
    from robothor.engine.refusal import refusal

    return refusal(
        f"The Datadog query was refused: {reason}.",
        gate="policy",
        name="datadog_query",
        unlock="Rewrite the query so its brackets, parentheses and quotes balance, then retry.",
    )


def _failed(tool: str, exc: DatadogError) -> dict[str, Any]:
    """The kind is the whole explanation: never a URL, a key or a vendor body."""
    return {
        "error": f"{tool} failed: Datadog answered {exc.kind}.",
        "kind": exc.kind,
        "retryable": exc.kind in ("rate_limit", "network"),
    }


# ── the audit row ──────────────────────────────────────────────────────────


def _audit(tool: str, ctx: ToolContext, query: str, details: dict[str, Any], status: str) -> None:
    """One ``datadog.logs_read`` row, queued off the loop. Never raises.

    Its own event rather than extra fields on ``agent.tool_call``: that row
    deliberately carries no arguments, and there is no hook for a handler to
    add any. Under ``ctx.tenant_id``, the tenant whose logs were read; a
    cross-tenant call also leaves ``agent.cross_tenant_call`` under the home
    tenant.
    """
    try:
        from robothor.audit.logger import log_event
        from robothor.engine.event_writer import get_event_writer
        from robothor.secrets.redaction import redact

        shown = redact_personal_data(redact(query[:4096]))[:_AUDIT_QUERY_CHARS]
        get_event_writer().submit(
            log_event,
            event_type=AUDIT_EVENT,
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


# ── arguments ──────────────────────────────────────────────────────────────


def _text(args: dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _count(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _facets(args: dict[str, Any]) -> list[str] | None:
    raw = args.get("group_by")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(f, str) and f.strip() for f in raw):
        return None
    return [f.strip() for f in raw]


# ── shaping and fitting ────────────────────────────────────────────────────


def _masked(value: Any) -> Any:
    if isinstance(value, str):
        return redact_personal_data(value)
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def _shape(row: LogRow) -> dict[str, Any]:
    tags: dict[str, str] = {}
    for tag in row.tags:
        key, _, value = tag.partition(":")
        if key in _ROW_TAGS and value and key not in tags:
            tags[key] = redact_personal_data(value)
    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "service": row.service,
        "status": row.status,
        "host": row.host,
        "message": redact_personal_data(row.message),
        "tags": tags,
        "attributes": _masked(row.attributes),
    }


def _size(payload: Any) -> int:
    return len(json.dumps(payload, default=str))


def _cap_message(row: dict[str, Any], cap: int) -> dict[str, Any]:
    message = row["message"]
    if len(message) <= cap:
        return row
    return {**row, "message": message[:cap], "message_truncated": True}


def _fit(rows: list[dict[str, Any]], room: int) -> list[dict[str, Any]]:
    """``rows`` made to fit ``room`` characters, losing as little as possible.

    Long messages are cut first, then attributes are dropped, then trailing
    rows: never a middle one, so the next page starts where this one stops.
    At least one row is always kept.
    """
    for cap in _MESSAGE_CAPS:
        if _size(rows) <= room:
            return rows
        rows = [_cap_message(r, cap) for r in rows]
    if _size(rows) > room:
        rows = [
            {**{k: v for k, v in r.items() if k != "attributes"}, "attributes_omitted": True}
            if r.get("attributes")
            else r
            for r in rows
        ]
    while len(rows) > 1 and _size(rows) > room:
        rows.pop()
    return rows


async def _client() -> AsyncDatadogClient:
    return await asyncio.to_thread(_client_factory)


# ── the tools ──────────────────────────────────────────────────────────────


@_handler(SEARCH_TOOL)
async def _datadog_logs_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log lines matching ``query`` inside this tenant's scope, newest first by default."""
    query = _text(args, "query")
    start, end = _text(args, "start", DEFAULT_START), _text(args, "end", DEFAULT_END)
    limit = _count(args, "limit", DEFAULT_SEARCH_LIMIT)
    cursor = _text(args, "cursor") or None
    newest_first = args.get("newest_first") is not False
    window = {"start": start, "end": end}

    scope = await asyncio.to_thread(scope_for, ctx.tenant_id)
    if scope is None:
        _audit(SEARCH_TOOL, ctx, query, {**window, "rows": 0}, "denied")
        return _not_configured()
    try:
        compose(scope.logs, query)  # judged before any client is built
        client = await _client()
        page = await client.search_logs(
            scope,
            query,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            newest_first=newest_first,
        )
    except ScopeError as exc:
        _audit(SEARCH_TOOL, ctx, query, {**window, "rows": 0}, "denied")
        return _query_refused(exc.reason)
    except DatadogError as exc:
        _audit(SEARCH_TOOL, ctx, query, {**window, "rows": 0, "kind": exc.kind}, "error")
        return _failed(SEARCH_TOOL, exc)

    shaped = [_shape(row) for row in page.rows]
    # Room for the envelope around the rows: the note, the cursor, the window.
    kept = _fit(shaped, RESULT_BUDGET_CHARS - 1000)
    next_cursor = page.next_cursor
    result: dict[str, Any] = {"scope": ctx.tenant_id, "window": window, "query": query}
    if len(kept) < len(shaped):
        # Datadog's cursor continues after the LAST row it sent. Asking again
        # for exactly the rows kept gives the cursor that continues after them.
        try:
            again = await client.search_logs(
                scope,
                query,
                start=start,
                end=end,
                limit=len(kept),
                cursor=cursor,
                newest_first=newest_first,
            )
            next_cursor = again.next_cursor
        except DatadogError:
            next_cursor = None
        result["note"] = f"{len(kept)} of {len(shaped)} rows returned to fit the result; " + (
            "pass next_cursor with the same query and window for the rest."
            if next_cursor
            else "narrow the query or window to see the rest."
        )
    trimmed = len(kept) < len(shaped) or kept != shaped
    if trimmed and "note" not in result:
        result["note"] = (
            f"{len(kept)} rows returned with long messages or attributes shortened to fit."
        )
    result.update(
        rows=kept,
        rows_returned=len(kept),
        next_cursor=next_cursor,
        truncated=trimmed,
    )
    _audit(
        SEARCH_TOOL,
        ctx,
        query,
        {**window, "rows": len(kept), "truncated": trimmed},
        "ok",
    )
    return result


@_handler(AGGREGATE_TOOL)
async def _datadog_logs_aggregate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log counts matching ``query`` inside this tenant's scope, grouped by facets."""
    query = _text(args, "query")
    start, end = _text(args, "start", DEFAULT_START), _text(args, "end", DEFAULT_END)
    limit = _count(args, "limit", DEFAULT_AGGREGATE_LIMIT)
    window = {"start": start, "end": end}
    group_by = _facets(args)
    if group_by is None:
        return {"error": "group_by must be a list of facet names, such as ['service', 'status']."}
    audited = {**window, "group_by": group_by}

    scope = await asyncio.to_thread(scope_for, ctx.tenant_id)
    if scope is None:
        _audit(AGGREGATE_TOOL, ctx, query, {**audited, "rows": 0}, "denied")
        return _not_configured()
    try:
        compose(scope.logs, query)
        client = await _client()
        buckets = await client.aggregate_logs(
            scope, query, start=start, end=end, group_by=group_by, limit=limit
        )
    except ScopeError as exc:
        _audit(AGGREGATE_TOOL, ctx, query, {**audited, "rows": 0}, "denied")
        return _query_refused(exc.reason)
    except DatadogError as exc:
        _audit(AGGREGATE_TOOL, ctx, query, {**audited, "rows": 0, "kind": exc.kind}, "error")
        return _failed(AGGREGATE_TOOL, exc)

    rows = [
        {"by": {k: redact_personal_data(v) for k, v in b.by.items()}, "count": b.count}
        for b in buckets
    ]
    kept = list(rows)
    while len(kept) > 1 and _size(kept) > RESULT_BUDGET_CHARS - 1000:
        kept.pop()
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "window": window,
        "query": query,
        "group_by": group_by,
        "buckets": kept,
        "total": sum(b["count"] for b in rows),
        "truncated": len(kept) < len(rows),
    }
    if result["truncated"]:
        result["note"] = (
            f"{len(kept)} of {len(rows)} groups returned to fit the result, largest first; "
            "narrow the query or group by fewer facets for the rest."
        )
    _audit(AGGREGATE_TOOL, ctx, query, {**audited, "rows": len(rows)}, "ok")
    return result
