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
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.integrations.datadog.client import (
    AsyncDatadogClient,
    DatadogError,
    async_client_from_settings,
)
from robothor.integrations.datadog.scope import ScopeError, compose, scope_for
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.datadog.requests import Bucket, LogPage, LogRow
    from robothor.integrations.datadog.scope import DatadogScope

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


def _now() -> datetime:
    """The moment a relative window is resolved against. Tests fix it."""
    return datetime.now(UTC)


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


#: Datadog's relative times: ``now``, or ``now-<n><unit>``.
_RELATIVE = re.compile(r"now(?:-(\d+)([smhdw]))?")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _absolute(text: str, now: datetime) -> str:
    """``text`` as an ISO-8601 time when it is a relative time, else unchanged.

    A relative window moves between calls, so a cursor handed out with one
    would page through a different set of logs than the page it came from.
    Resolved once here and returned in ``window``, the model passes the same
    absolute window back with the cursor.
    """
    match = _RELATIVE.fullmatch(text.strip().lower())
    if match is None:
        return text
    amount, unit = match.groups()
    moment = now - timedelta(**{_UNITS[unit]: int(amount)}) if unit else now
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _window(args: dict[str, Any]) -> dict[str, str]:
    """The call's ``{start, end}``, relative times resolved against one ``now``."""
    now = _now()
    return {
        "start": _absolute(_text(args, "start", DEFAULT_START), now),
        "end": _absolute(_text(args, "end", DEFAULT_END), now),
    }


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


#: Attribute keys whose value is a phone number whatever its shape: a number
#: stored as an integer, or as bare digits, matches no phone pattern.
_PHONE_KEY = re.compile(
    r"phone|mobile|msisdn|(?:^|[_.-])(?:tel|fax|cell)(?:$|[_.-])", re.IGNORECASE
)
_DIGITS_IN_A_PHONE = 7


def _masked(value: Any, key: str = "") -> Any:
    """``value`` with email addresses and phone numbers masked, at any depth."""
    if _PHONE_KEY.search(key) and (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, str) and sum(c.isdigit() for c in value) >= _DIGITS_IN_A_PHONE)
    ):
        return "<phone>"
    if isinstance(value, str):
        return redact_personal_data(value)
    if isinstance(value, dict):
        return {k: _masked(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v, key) for v in value]
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


async def _scoped(
    tool: str,
    ctx: ToolContext,
    query: str,
    audited: dict[str, Any],
    call: Callable[[AsyncDatadogClient, DatadogScope], Awaitable[Any]],
) -> tuple[Any, dict[str, Any] | None]:
    """``call`` under this tenant's scope: (its value, None), or (None, the answer).

    The scope comes from ``ctx.tenant_id`` and nowhere else. The query is
    judged before the client is built, so an escape is refused even on an
    instance with no key, and nothing is sent for a refusal.
    """
    scope = await asyncio.to_thread(scope_for, ctx.tenant_id)
    try:
        if scope is not None:
            # The scope alone: a tenant whose entry grants no log access is
            # not configured for logs, whatever the query says.
            compose(scope.logs, "")
    except ScopeError:
        scope = None
    if scope is None:
        _audit(tool, ctx, query, {**audited, "rows": 0}, "denied")
        return None, _not_configured()
    try:
        compose(scope.logs, query)
        return await call(await _client(), scope), None
    except ScopeError as exc:
        _audit(tool, ctx, query, {**audited, "rows": 0}, "denied")
        return None, _query_refused(exc.reason)
    except DatadogError as exc:
        _audit(tool, ctx, query, {**audited, "rows": 0, "kind": exc.kind}, "error")
        return None, _failed(tool, exc)


# ── the tools ──────────────────────────────────────────────────────────────


@_handler(SEARCH_TOOL)
async def _datadog_logs_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log lines matching ``query`` inside this tenant's scope, newest first by default."""
    query = _text(args, "query")
    window = _window(args)
    start, end = window["start"], window["end"]
    limit = _count(args, "limit", DEFAULT_SEARCH_LIMIT)
    cursor = _text(args, "cursor") or None
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
        kept = _fit(shaped, RESULT_BUDGET_CHARS - 1000)
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
    _audit(SEARCH_TOOL, ctx, query, {**window, "rows": len(kept), "truncated": truncated}, "ok")
    return result


@_handler(AGGREGATE_TOOL)
async def _datadog_logs_aggregate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Log counts matching ``query`` inside this tenant's scope, grouped by facets."""
    query = _text(args, "query")
    window = _window(args)
    start, end = window["start"], window["end"]
    limit = _count(args, "limit", DEFAULT_AGGREGATE_LIMIT)
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
    while len(kept) > 1 and _size(kept) > RESULT_BUDGET_CHARS - 1000:
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
    _audit(AGGREGATE_TOOL, ctx, query, {**audited, "rows": len(rows)}, "ok")
    return result
