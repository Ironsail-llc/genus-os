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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.handlers.datadog_common import (
    ENVELOPE_CHARS,
    RESULT_BUDGET_CHARS,
    audit,
    count,
    masked,
    resolve_window,
    scoped,
    size,
    text,
)
from robothor.integrations.datadog.client import (
    AsyncDatadogClient,
    DatadogError,
    async_client_from_settings,
)
from robothor.integrations.datadog.scope import compose
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.datadog.requests import Bucket, LogPage, LogRow
    from robothor.integrations.datadog.scope import DatadogScope

HANDLERS: dict[str, Any] = {}

SEARCH_TOOL = "datadog_logs_search"
AGGREGATE_TOOL = "datadog_logs_aggregate"
AUDIT_EVENT = "datadog.logs_read"

DEFAULT_START = "now-1h"
DEFAULT_END = "now"
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_AGGREGATE_LIMIT = 25

# RESULT_BUDGET_CHARS (shared) is how much a call hands the model: a page that
# does not fit is cut at a row boundary and ``next_cursor`` continues from the
# first row left out.

#: Successive message lengths tried when a page does not fit, longest first.
_MESSAGE_CAPS = (2000, 500, 200)
#: The tags every row lifts out of its tag list.
_ROW_TAGS = ("env", "version")

#: Builds the client. Blocking (the vault read): always called through
#: ``asyncio.to_thread``. Tests replace it with a client over a fake transport.
_client_factory: Callable[[], AsyncDatadogClient] = async_client_from_settings


def _now() -> datetime:
    """The moment a relative window is resolved against. Tests fix it."""
    return datetime.now(UTC)


def _window(args: dict[str, Any]) -> dict[str, str]:
    """The call's ``{start, end}``, relative times resolved against one ``now``."""
    return resolve_window(args, _now(), DEFAULT_START, DEFAULT_END)


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


# ── arguments ──────────────────────────────────────────────────────────────


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
        "attributes": masked(row.attributes),
    }


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
        if size(rows) <= room:
            return rows
        rows = [_cap_message(r, cap) for r in rows]
    if size(rows) > room:
        rows = [
            {**{k: v for k, v in r.items() if k != "attributes"}, "attributes_omitted": True}
            if r.get("attributes")
            else r
            for r in rows
        ]
    while len(rows) > 1 and size(rows) > room:
        rows.pop()
    return rows


async def _scoped(
    tool: str,
    ctx: ToolContext,
    query: str,
    audited: dict[str, Any],
    call: Callable[[AsyncDatadogClient, DatadogScope], Awaitable[Any]],
) -> tuple[Any, dict[str, Any] | None]:
    """``call`` under this tenant's log scope, through the shared scoped-call path."""
    return await scoped(
        tool,
        AUDIT_EVENT,
        ctx,
        query,
        audited,
        call,
        part=lambda scope: compose(scope.logs, ""),
        check=lambda scope: compose(scope.logs, query),
        build=lambda: _client_factory(),
    )


# ── the tools ──────────────────────────────────────────────────────────────


@_handler(SEARCH_TOOL)
async def _datadog_logs_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log lines matching ``query`` inside this tenant's scope, newest first by default."""
    query = text(args, "query")
    window = _window(args)
    start, end = window["start"], window["end"]
    limit = count(args, "limit", DEFAULT_SEARCH_LIMIT)
    cursor = text(args, "cursor") or None
    newest_first = args.get("newest_first") is not False

    async def fetch(
        client: AsyncDatadogClient, scope: DatadogScope
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        async def page(size: int) -> LogPage:
            return await client.search_logs(
                scope,
                query,
                start=start,
                end=end,
                limit=size,
                cursor=cursor,
                newest_first=newest_first,
            )

        first = await page(limit)
        shaped = [_shape(row) for row in first.rows]
        # Room for the envelope around the rows: the note, the cursor, the window.
        kept = _fit(shaped, RESULT_BUDGET_CHARS - ENVELOPE_CHARS)
        if len(kept) == len(shaped):
            return shaped, kept, first.next_cursor
        # Datadog's cursor continues after the LAST row it sent. Asking again
        # for exactly the rows kept gives the cursor that continues after them,
        # but only if that page IS the rows kept: logs arriving in between
        # would shift it, and its cursor would then skip or repeat rows.
        try:
            second = await page(len(kept))
        except DatadogError:
            return shaped, kept, None
        if [row.id for row in second.rows] != [row["id"] for row in kept]:
            return shaped, kept, None
        return shaped, kept, second.next_cursor

    got, refused = await _scoped(SEARCH_TOOL, ctx, query, window, fetch)
    if refused is not None:
        return refused
    shaped, kept, next_cursor = got
    truncated = kept != shaped
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "window": window,
        "query": query,
        "rows": kept,
        "rows_returned": len(kept),
        "next_cursor": next_cursor,
        "truncated": truncated,
    }
    if len(kept) < len(shaped):
        rest = (
            "pass next_cursor with the same query and window for the rest."
            if next_cursor
            else "narrow the query or window to see the rest."
        )
        result["note"] = f"{len(kept)} of {len(shaped)} rows returned to fit the result; {rest}"
    elif truncated:
        result["note"] = (
            f"{len(kept)} rows returned with long messages or attributes shortened to fit."
        )
    audit(
        AUDIT_EVENT,
        SEARCH_TOOL,
        ctx,
        query,
        {**window, "rows": len(kept), "truncated": truncated},
        "ok",
    )
    return result


@_handler(AGGREGATE_TOOL)
async def _datadog_logs_aggregate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log counts matching ``query`` inside this tenant's scope, grouped by facets."""
    query = text(args, "query")
    window = _window(args)
    start, end = window["start"], window["end"]
    limit = count(args, "limit", DEFAULT_AGGREGATE_LIMIT)
    group_by = _facets(args)
    if group_by is None:
        return {"error": "group_by must be a list of facet names, such as ['service', 'status']."}
    audited = {**window, "group_by": group_by}

    async def fetch(client: AsyncDatadogClient, scope: DatadogScope) -> tuple[Bucket, ...]:
        return await client.aggregate_logs(
            scope, query, start=start, end=end, group_by=group_by, limit=limit
        )

    buckets, refused = await _scoped(AGGREGATE_TOOL, ctx, query, audited, fetch)
    if refused is not None:
        return refused
    rows = [
        {"by": {k: redact_personal_data(v) for k, v in b.by.items()}, "count": b.count}
        for b in buckets
    ]
    kept = list(rows)
    while len(kept) > 1 and size(kept) > RESULT_BUDGET_CHARS - ENVELOPE_CHARS:
        kept.pop()
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "window": window,
        "query": query,
        "group_by": group_by,
        "buckets": kept,
        "truncated": len(kept) < len(rows),
    }
    if result["truncated"]:
        result["note"] = (
            f"{len(kept)} of {len(rows)} groups returned to fit the result; "
            "narrow the query or group by fewer facets for the rest."
        )
    audit(AUDIT_EVENT, AGGREGATE_TOOL, ctx, query, {**audited, "rows": len(rows)}, "ok")
    return result
