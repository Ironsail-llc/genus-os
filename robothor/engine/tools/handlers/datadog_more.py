"""Datadog span, trace, monitor and metric tools: a tenant reads its OWN, read-only.

The siblings of :mod:`.datadog_logs`, and the same wall: the scope comes from
``ctx.tenant_id`` alone, through the one scoped-call path in
:mod:`.datadog_common`. Each kind of data is bounded by its own part of the
tenant's scope (``spans``, ``monitors``, ``metric_tags``); a tenant whose part
is "no access" is refused before anything is sent.

* ``datadog_trace_get`` is a span search for ``trace_id:<id>`` inside the
  span filter, so a trace that belongs to another tenant matches nothing and
  reads as "not found", never as an error that says it exists.
* ``datadog_metrics_query`` takes structured parts only; the client builds
  the query with the tenant's tags always first in the filter.

Span resource names, monitor names and every tag pass through
:func:`~robothor.secrets.redaction.redact_personal_data`. Each call writes one
audit row (``datadog.spans_read``, ``datadog.trace_read``,
``datadog.monitors_read``, ``datadog.metrics_read``) with the query, trace id
or metric name, the window and the counts; never span or monitor text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.handlers.datadog_common import (
    ENVELOPE_CHARS,
    RESULT_BUDGET_CHARS,
    audit,
    count,
    masked_tags,
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
from robothor.integrations.datadog.requests import MAX_PAGE
from robothor.integrations.datadog.scope import (
    ScopeError,
    compose,
    compose_metric,
    trace_query,
)
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.datadog.requests import (
        MetricSeries,
        MonitorPage,
        MonitorRow,
        SpanPage,
        SpanRow,
    )
    from robothor.integrations.datadog.scope import DatadogScope

__all__ = ["HANDLERS", "RESULT_BUDGET_CHARS"]

HANDLERS: dict[str, Any] = {}

SPANS_TOOL = "datadog_spans_search"
TRACE_TOOL = "datadog_trace_get"
MONITORS_TOOL = "datadog_monitors_list"
METRICS_TOOL = "datadog_metrics_query"

SPANS_EVENT = "datadog.spans_read"
TRACE_EVENT = "datadog.trace_read"
MONITORS_EVENT = "datadog.monitors_read"
METRICS_EVENT = "datadog.metrics_read"

DEFAULT_START = "now-1h"
DEFAULT_END = "now"
#: A trace id usually comes from a log line read some time ago: a day back.
DEFAULT_TRACE_START = "now-1d"
DEFAULT_SPAN_LIMIT = 50
DEFAULT_MONITOR_LIMIT = 50

#: The monitor search filter ``only_firing`` adds: alerting, warning, no data.
FIRING_QUERY = 'status:alert OR status:warn OR status:"no data"'

#: Successive resource-name lengths tried when spans do not fit, longest first.
_RESOURCE_CAPS = (500, 200, 80)
#: Successive monitor-name lengths tried when monitors do not fit.
_NAME_CAPS = (500, 120)
#: The fewest points a down-sampled series keeps: first, last, min, max.
_MIN_POINTS = 4

_TRACE_UNLOCK = "Pass the trace id as hex or decimal digits, as a log line or span shows it."
_METRIC_UNLOCK = (
    "Use a metric name of letters, digits, dots and underscores, an aggregator of avg, sum, "
    "min or max, and tags such as service:shop-api, then retry."
)

#: Builds the client. Blocking (the vault read): always called through
#: ``asyncio.to_thread``. Tests replace it with a client over a fake transport.
_client_factory: Callable[[], AsyncDatadogClient] = async_client_from_settings


def _now() -> datetime:
    """The moment a relative window is resolved against. Tests fix it."""
    return datetime.now(UTC)


def _window(args: dict[str, Any], default_start: str = DEFAULT_START) -> dict[str, str]:
    """The call's ``{start, end}``, relative times resolved against one ``now``.

    Resolved once and handed back, so a cursor is used with the window that
    produced it and a metric is read over the window the result names.
    """
    return resolve_window(args, _now(), default_start, DEFAULT_END)


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


#: Each tool's own part of the scope, judged alone: a part that grants no
#: access refuses the tenant as not configured for that kind of data.
_PARTS: dict[str, Callable[[DatadogScope], object]] = {
    SPANS_TOOL: lambda scope: compose(scope.spans, ""),
    TRACE_TOOL: lambda scope: compose(scope.spans, ""),
    MONITORS_TOOL: lambda scope: compose(scope.monitors, ""),
    # Any valid metric name: what is judged is the tenant's tags.
    METRICS_TOOL: lambda scope: compose_metric(scope.metric_tags, "probe"),
}


async def _scoped(
    tool: str,
    event: str,
    ctx: ToolContext,
    query: str,
    audited: dict[str, Any],
    call: Callable[[AsyncDatadogClient, DatadogScope], Awaitable[Any]],
    check: Callable[[DatadogScope], object],
    unlock: str | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """``call`` under this tenant's scope, through the shared scoped-call path."""
    return await scoped(
        tool,
        event,
        ctx,
        query,
        audited,
        call,
        part=_PARTS[tool],
        check=check,
        build=lambda: _client_factory(),
        unlock=unlock,
    )


def _strings(args: dict[str, Any], key: str) -> list[str] | None:
    """A list of strings (a lone string counts as one); None when it is neither."""
    raw = args.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        return None
    return [v.strip() for v in raw]


# ── spans ──────────────────────────────────────────────────────────────────


def _shape_span(row: SpanRow, *, parent: bool = False) -> dict[str, Any]:
    shaped: dict[str, Any] = {
        "time": row.start_timestamp,
        "service": row.service,
        "resource": redact_personal_data(row.resource_name),
        "operation": row.operation_name,
        "duration_ms": None if row.duration_ns is None else round(row.duration_ns / 1e6, 3),
        "status": row.status,
        "error": row.status == "error",
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "env": redact_personal_data(row.env),
    }
    if parent:
        shaped["parent_id"] = row.parent_id
    return shaped


def _cap(row: dict[str, Any], key: str, cap: int) -> dict[str, Any]:
    value = row.get(key) or ""
    if len(value) <= cap:
        return row
    return {**row, key: value[:cap], f"{key}_truncated": True}


def _fit(rows: list[dict[str, Any]], key: str, caps: tuple[int, ...]) -> list[dict[str, Any]]:
    """``rows`` fitted to the budget: long ``key`` values cut first, then trailing rows.

    Never a middle row, so a next page starts where this one stops. At least
    one row is always kept.
    """
    room = RESULT_BUDGET_CHARS - ENVELOPE_CHARS
    for cap in caps:
        if size(rows) <= room:
            return rows
        rows = [_cap(r, key, cap) for r in rows]
    while len(rows) > 1 and size(rows) > room:
        rows.pop()
    return rows


def _fit_note(kept: int, total: int, noun: str, rest: str) -> str:
    if kept < total:
        return f"{kept} of {total} {noun} returned to fit the result; {rest}"
    return f"{kept} {noun} returned with long names shortened to fit."


@_handler(SPANS_TOOL)
async def _datadog_spans_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """APM spans matching ``query`` inside this tenant's span scope, newest first by default."""
    query = text(args, "query")
    window = _window(args)
    start, end = window["start"], window["end"]
    limit = count(args, "limit", DEFAULT_SPAN_LIMIT)
    cursor = text(args, "cursor") or None
    newest_first = args.get("newest_first") is not False

    async def fetch(
        client: AsyncDatadogClient, scope: DatadogScope
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        async def page(size_: int) -> SpanPage:
            return await client.search_spans(
                scope,
                query,
                start=start,
                end=end,
                limit=size_,
                cursor=cursor,
                newest_first=newest_first,
            )

        first = await page(limit)
        shaped = [_shape_span(row) for row in first.rows]
        kept = _fit(shaped, "resource", _RESOURCE_CAPS)
        if len(kept) == len(shaped):
            return shaped, kept, first.next_cursor
        # Datadog's cursor continues after the LAST span it sent: asking again
        # for exactly the spans kept gives the cursor that continues after them,
        # but only if that page IS the spans kept: spans arriving in between
        # would shift it, and its cursor would then skip or repeat spans.
        try:
            second = await page(len(kept))
        except DatadogError:
            return shaped, kept, None
        if [row.span_id for row in second.rows] != [row["span_id"] for row in kept]:
            return shaped, kept, None
        return shaped, kept, second.next_cursor

    got, refused = await _scoped(
        SPANS_TOOL,
        SPANS_EVENT,
        ctx,
        query,
        window,
        fetch,
        lambda scope: compose(scope.spans, query),
    )
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
    if truncated:
        rest = (
            "pass next_cursor with the same query and window for the rest."
            if next_cursor
            else "narrow the query or window to see the rest."
        )
        result["note"] = _fit_note(len(kept), len(shaped), "spans", rest)
    audit(SPANS_EVENT, SPANS_TOOL, ctx, query, {**window, "rows": len(kept)}, "ok")
    return result


# ── one trace ──────────────────────────────────────────────────────────────


@_handler(TRACE_TOOL)
async def _datadog_trace_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The spans of one trace, by start time with parent ids, inside the span scope."""
    raw_id = args.get("trace_id")
    trace_id = raw_id.strip(" ") if isinstance(raw_id, str) else ""
    window = _window(args, DEFAULT_TRACE_START)
    start, end = window["start"], window["end"]

    def check(scope: DatadogScope) -> object:
        return compose(scope.spans, trace_query(trace_id))

    async def fetch(client: AsyncDatadogClient, scope: DatadogScope) -> tuple[SpanRow, ...]:
        return await client.get_trace(scope, trace_id, start=start, end=end, limit=MAX_PAGE)

    rows, refused = await _scoped(
        TRACE_TOOL, TRACE_EVENT, ctx, trace_id, window, fetch, check, _TRACE_UNLOCK
    )
    if refused is not None:
        return refused
    ordered = sorted(rows, key=lambda r: r.start_timestamp)
    spans = [_shape_span(row, parent=True) for row in ordered]
    kept = _fit(spans, "resource", _RESOURCE_CAPS)
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "window": window,
        "trace_id": trace_id,
        "found": bool(spans),
        "spans": kept,
        "spans_returned": len(kept),
        "spans_total": len(spans),
        "truncated": kept != spans or len(rows) >= MAX_PAGE,
    }
    if not spans:
        result["note"] = (
            f"Trace {trace_id} was not found in your organization's traces between "
            f"{start} and {end}. Widen the window if the trace is older."
        )
    elif kept != spans:
        result["note"] = _fit_note(len(kept), len(spans), "spans", "the earliest spans are shown.")
    elif len(rows) >= MAX_PAGE:
        result["note"] = f"The trace may have more than the {MAX_PAGE} spans shown."
    audit(TRACE_EVENT, TRACE_TOOL, ctx, trace_id, {**window, "rows": len(spans)}, "ok")
    return result


# ── monitors ───────────────────────────────────────────────────────────────


def _when(epoch_seconds: int | None) -> str | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _shape_monitor(row: MonitorRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": redact_personal_data(row.name),
        "status": row.status,
        "type": row.type,
        "tags": masked_tags(row.tags),
        "last_triggered": _when(row.last_triggered_ts),
    }


def _monitor_query(query: str, only_firing: bool) -> str:
    if not only_firing:
        return query
    return f"({query}) AND ({FIRING_QUERY})" if query else FIRING_QUERY


@_handler(MONITORS_TOOL)
async def _datadog_monitors_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Monitors matching ``query`` inside this tenant's monitor scope, with their status."""
    only_firing = args.get("only_firing") is True
    query = _monitor_query(text(args, "query"), only_firing)
    limit = count(args, "limit", DEFAULT_MONITOR_LIMIT)
    page_arg = args.get("page")
    page = page_arg if isinstance(page_arg, int) and not isinstance(page_arg, bool) else 0
    page = max(0, page)

    async def fetch(client: AsyncDatadogClient, scope: DatadogScope) -> MonitorPage:
        return await client.search_monitors(scope, query, limit=limit, page=page)

    got, refused = await _scoped(
        MONITORS_TOOL,
        MONITORS_EVENT,
        ctx,
        query,
        {"only_firing": only_firing, "page": page},
        fetch,
        lambda scope: compose(scope.monitors, query),
    )
    if refused is not None:
        return refused
    shaped = [_shape_monitor(row) for row in got.rows]
    kept = _fit(shaped, "name", _NAME_CAPS)
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "query": query,
        "monitors": kept,
        "total_count": got.total_count,
        "page": got.page,
        "page_count": got.page_count,
        "next_page": got.page + 1 if got.page + 1 < got.page_count else None,
        "truncated": kept != shaped,
    }
    if result["truncated"]:
        result["note"] = _fit_note(
            len(kept), len(shaped), "monitors", "pass a smaller limit to page through the rest."
        )
    audited = {"only_firing": only_firing, "page": page, "rows": len(shaped)}
    audit(MONITORS_EVENT, MONITORS_TOOL, ctx, query, audited, "ok")
    return result


# ── metrics ────────────────────────────────────────────────────────────────


def _sample(points: list[list[float]], n: int) -> list[list[float]]:
    """At most ``n`` of ``points`` in time order: first, last, min, max and an even spread."""
    if len(points) <= n:
        return points
    values = [p[1] for p in points]
    keep = {0, len(points) - 1, values.index(min(values)), values.index(max(values))}
    spread = max(n - len(keep), 0)
    if spread:
        step = (len(points) - 1) / (spread + 1)
        keep.update(round(step * (i + 1)) for i in range(spread))
    return [points[i] for i in sorted(keep)]


def _shape_series(series: MetricSeries) -> dict[str, Any]:
    return {
        "metric": series.metric,
        "scope": redact_personal_data(series.scope),
        "tags": masked_tags(series.tags),
        "points": [[ts, value] for ts, value in series.points],
    }


def _fit_series(shaped: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """The series fitted to the budget by down-sampling, then dropping trailing series."""
    room = RESULT_BUDGET_CHARS - ENVELOPE_CHARS
    if size(shaped) <= room:
        return shaped, False
    totals = [len(s["points"]) for s in shaped]
    target = max(totals)
    fitted = shaped
    while target > _MIN_POINTS and size(fitted) > room:
        target = max(_MIN_POINTS, target // 2)
        fitted = [
            {**s, "points": _sample(s["points"], target), "points_total": total}
            if total > target
            else s
            for s, total in zip(shaped, totals, strict=True)
        ]
    while len(fitted) > 1 and size(fitted) > room:
        fitted = fitted[:-1]
    return fitted, True


@_handler(METRICS_TOOL)
async def _datadog_metrics_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """One metric over a window, built from parts inside this tenant's metric tags."""
    metric = text(args, "metric")
    aggregator = text(args, "aggregator", "avg")
    tags, group_by = _strings(args, "tags"), _strings(args, "group_by")
    window = _window(args)
    start, end = window["start"], window["end"]
    as_count = args.get("as_count") is True
    audited = {**window, "aggregator": aggregator, "series": 0, "points": 0}

    def check(scope: DatadogScope) -> object:
        if tags is None or group_by is None:
            raise ScopeError("tags and group_by are lists of strings such as service:shop-api")
        return compose_metric(
            scope.metric_tags,
            metric,
            aggregator=aggregator,
            tags=tags,
            group_by=group_by,
            as_count=as_count,
        )

    async def fetch(client: AsyncDatadogClient, scope: DatadogScope) -> tuple[MetricSeries, ...]:
        return await client.query_metric(
            scope,
            metric,
            aggregator=aggregator,
            tags=tags or (),
            group_by=group_by or (),
            start=start,
            end=end,
            as_count=as_count,
        )

    got, refused = await _scoped(
        METRICS_TOOL, METRICS_EVENT, ctx, metric, audited, fetch, check, _METRIC_UNLOCK
    )
    if refused is not None:
        return refused
    shaped = [_shape_series(s) for s in got]
    kept, truncated = _fit_series(shaped)
    result: dict[str, Any] = {
        "scope": ctx.tenant_id,
        "window": window,
        "metric": metric,
        "aggregator": aggregator,
        "series": kept,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"{len(kept)} of {len(shaped)} series returned, long series down-sampled (first, "
            "last, min, max and an even spread kept; points_total is the full count) to fit "
            "the result. Narrow the window or group by fewer keys for every point."
        )
    points = sum(len(s["points"]) for s in shaped)
    audited = {**audited, "series": len(shaped), "points": points}
    audit(METRICS_EVENT, METRICS_TOOL, ctx, metric, audited, "ok")
    return result
