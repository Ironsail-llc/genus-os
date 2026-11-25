"""Datadog API requests and responses as plain data: builders and parsers, no I/O.

The clients in :mod:`.client` are thin shells over these. Parsers are
defensive: a missing or oddly typed field becomes an empty value, never a
``KeyError``, because a vendor response is input, not a contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "KNOWN_SITES",
    "MAX_PAGE",
    "ApiRequest",
    "Bucket",
    "LogPage",
    "LogRow",
    "MetricSeries",
    "MonitorPage",
    "MonitorRow",
    "SpanPage",
    "SpanRow",
    "api_base",
    "logs_aggregate",
    "logs_search",
    "metrics_query",
    "monitors_search",
    "parse_buckets",
    "parse_log_page",
    "parse_metric_series",
    "parse_monitor_page",
    "parse_span_buckets",
    "parse_span_page",
    "resolve_epoch",
    "spans_aggregate",
    "spans_search",
    "validate_request",
]

#: The Datadog sites a key may be sent to. Exact names, not a pattern: a
#: suffix match would let ``datadoghq.com.example.com`` through.
KNOWN_SITES = frozenset(
    {
        "datadoghq.com",
        "us3.datadoghq.com",
        "us5.datadoghq.com",
        "datadoghq.eu",
        "ap1.datadoghq.com",
        "ap2.datadoghq.com",
        "ddog-gov.com",
        "us2.ddog-gov.com",
    }
)
#: The log search API's own page bound. Fitting the request to it avoids a
#: 400; it is not a limit on how much a caller may read (it pages on).
MAX_PAGE = 1000


@dataclass(frozen=True)
class ApiRequest:
    method: Literal["GET", "POST"]
    path: str
    body: dict[str, Any] | None = None
    params: dict[str, str] | None = None


@dataclass(frozen=True)
class LogRow:
    id: str
    timestamp: str
    service: str
    status: str
    host: str
    message: str
    tags: tuple[str, ...]
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Last and defaulted, so a constructor written before they existed still works.
    trace_id: str = ""
    span_id: str = ""


@dataclass(frozen=True)
class LogPage:
    rows: tuple[LogRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class Bucket:
    by: dict[str, str]
    count: int


@dataclass(frozen=True)
class SpanRow:
    trace_id: str
    span_id: str
    parent_id: str
    service: str
    resource_name: str
    operation_name: str
    start_timestamp: str
    end_timestamp: str
    duration_ns: int | None
    status: str
    env: str
    tags: tuple[str, ...]
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanPage:
    rows: tuple[SpanRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class MonitorRow:
    id: str
    name: str
    status: str
    type: str
    tags: tuple[str, ...]
    last_triggered_ts: int | None


@dataclass(frozen=True)
class MonitorPage:
    rows: tuple[MonitorRow, ...]
    total_count: int
    page: int
    page_count: int


@dataclass(frozen=True)
class MetricSeries:
    metric: str
    scope: str
    tags: tuple[str, ...]
    #: (epoch milliseconds, value); null points are left out.
    points: tuple[tuple[int, float], ...]


def api_base(site: str) -> str | None:
    """``https://api.<site>`` for a known Datadog site, else None.

    Accepts the site as written in the docs (``datadoghq.eu``) or copied from
    the browser (``https://app.datadoghq.eu/``).
    """
    text = (site or "").strip().lower().removeprefix("https://").rstrip("/")
    for prefix in ("app.", "api."):
        text = text.removeprefix(prefix)
    return f"https://api.{text}" if text in KNOWN_SITES else None


def _filter(query: str, start: str, end: str) -> dict[str, str]:
    return {"query": query, "from": start, "to": end}


def _search_body(
    query: str, start: str, end: str, limit: int, cursor: str | None, newest_first: bool
) -> dict[str, Any]:
    page: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_PAGE))}
    if cursor:
        page["cursor"] = cursor
    return {
        "filter": _filter(query, start, end),
        "sort": "-timestamp" if newest_first else "timestamp",
        "page": page,
    }


def _aggregate_body(
    query: str, start: str, end: str, group_by: Sequence[str], limit: int
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "filter": _filter(query, start, end),
        "compute": [{"aggregation": "count"}],
    }
    if group_by:
        sort = {"aggregation": "count", "order": "desc", "type": "measure"}
        body["group_by"] = [
            {"facet": facet, "limit": max(1, int(limit)), "sort": dict(sort)} for facet in group_by
        ]
    return body


def logs_search(
    query: str,
    *,
    start: str,
    end: str,
    limit: int = 50,
    cursor: str | None = None,
    newest_first: bool = True,
) -> ApiRequest:
    """``POST /api/v2/logs/events/search`` for an already composed query."""
    body = _search_body(query, start, end, limit, cursor, newest_first)
    return ApiRequest("POST", "/api/v2/logs/events/search", body)


def logs_aggregate(
    query: str, *, start: str, end: str, group_by: Sequence[str], limit: int = 25
) -> ApiRequest:
    """``POST /api/v2/logs/analytics/aggregate``: a count, grouped by each facet."""
    body = _aggregate_body(query, start, end, group_by, limit)
    return ApiRequest("POST", "/api/v2/logs/analytics/aggregate", body)


#: A log filter no line carries: the application-key probe reads no tenant data.
APP_KEY_PROBE_QUERY = "genus_scope:no_access"


def app_key_probe_request() -> ApiRequest:
    """A one-row log search that matches nothing: Datadog checks the application key for it."""
    body = {
        "filter": {"query": APP_KEY_PROBE_QUERY, "from": "now-1m", "to": "now"},
        "page": {"limit": 1},
    }
    return ApiRequest("POST", "/api/v2/logs/events/search", body)


def spans_search(
    query: str,
    *,
    start: str,
    end: str,
    limit: int = 50,
    cursor: str | None = None,
    newest_first: bool = True,
) -> ApiRequest:
    """``POST /api/v2/spans/events/search``: the log search body, JSON:API-wrapped."""
    attributes = _search_body(query, start, end, limit, cursor, newest_first)
    body = {"data": {"type": "search_request", "attributes": attributes}}
    return ApiRequest("POST", "/api/v2/spans/events/search", body)


def spans_aggregate(
    query: str, *, start: str, end: str, group_by: Sequence[str], limit: int = 25
) -> ApiRequest:
    """``POST /api/v2/spans/analytics/aggregate``: a count, grouped by each facet."""
    attributes = _aggregate_body(query, start, end, group_by, limit)
    body = {"data": {"type": "aggregate_request", "attributes": attributes}}
    return ApiRequest("POST", "/api/v2/spans/analytics/aggregate", body)


def monitors_search(query: str, *, page: int = 0, limit: int = 50) -> ApiRequest:
    """``GET /api/v1/monitor/search`` for an already composed query; pages count from 0."""
    params = {"query": query, "page": str(max(0, int(page))), "per_page": str(max(1, int(limit)))}
    return ApiRequest("GET", "/api/v1/monitor/search", params=params)


def metrics_query(query: str, *, start: int, end: int) -> ApiRequest:
    """``GET /api/v1/query`` for a built metric query over epoch seconds."""
    params = {"from": str(int(start)), "to": str(int(end)), "query": query}
    return ApiRequest("GET", "/api/v1/query", params=params)


def validate_request() -> ApiRequest:
    """``GET /api/v1/validate``: checks the API key (Datadog ignores the application key)."""
    return ApiRequest("GET", "/api/v1/validate")


# ── the metric time window ──────────────────────────────────────────────────

_RELATIVE = re.compile(r"now(?:-([0-9]+)([smhd]))?")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def resolve_epoch(value: str, *, now: float) -> int:
    """Epoch seconds for ISO-8601, ``now`` or ``now-<n><s|m|h|d>``; raises ValueError.

    The v1 metric query takes epoch seconds, not Datadog's relative strings,
    so the window is resolved here. A naive ISO time is read as UTC.
    """
    text = (value or "").strip()
    relative = _RELATIVE.fullmatch(text)
    if relative is not None:
        amount, unit = relative.groups()
        return int(now) - (int(amount) * _UNIT_SECONDS[unit] if unit else 0)
    if _ISO_DATE.match(text):
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            pass
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return int(moment.timestamp())
    raise ValueError("a time is ISO-8601, now, or now-<n> with s, m, h or d")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return "" if value is None or isinstance(value, dict | list) else str(value)


#: A trace or span id: hex (a W3C/OTel id) or decimal (a Datadog 64-bit id),
#: at most 40 characters. ASCII only -- ``\d`` would also match other scripts'
#: digits. The fallbacks read CUSTOM attributes, which are free text, and every
#: consumer returns these fields unmasked, so anything else is dropped.
_ID = re.compile(r"[0-9a-f]{1,40}")


def _id_text(value: Any) -> str:
    """An id-shaped value in lowercase, or "" (``dd.trace_id`` can be an integer)."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    text = str(value).lower()
    return text if _ID.fullmatch(text) else ""


def _correlation_id(event: dict[str, Any], name: str) -> str:
    """``trace_id`` or ``span_id``: the event's reserved top-level field first, then
    the custom attribute forms ``<name>``, ``dd.<name>`` and ``otel.<name>``."""
    custom = _dict(event.get("attributes"))
    for value in (
        event.get(name),
        custom.get(name),
        _dict(custom.get("dd")).get(name),
        _dict(custom.get("otel")).get(name),
    ):
        text = _id_text(value)
        if text:
            return text
    return ""


def _row(raw: dict[str, Any]) -> LogRow:
    attrs = _dict(raw.get("attributes"))
    tags = attrs.get("tags")
    return LogRow(
        trace_id=_correlation_id(attrs, "trace_id"),
        span_id=_correlation_id(attrs, "span_id"),
        id=_str(raw.get("id")),
        timestamp=_str(attrs.get("timestamp")),
        service=_str(attrs.get("service")),
        status=_str(attrs.get("status")),
        host=_str(attrs.get("host")),
        message=_str(attrs.get("message")),
        tags=tuple(str(t) for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        attributes=_dict(attrs.get("attributes")),
    )


def parse_log_page(body: Any) -> LogPage:
    """The rows and the next cursor (``meta.page.after``) of a log search response."""
    body = _dict(body)
    data = body.get("data")
    rows = (
        tuple(_row(item) for item in data if isinstance(item, dict))
        if isinstance(data, list)
        else ()
    )
    after = _dict(_dict(body.get("meta")).get("page")).get("after")
    return LogPage(rows=rows, next_cursor=after if isinstance(after, str) and after else None)


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_buckets(body: Any) -> tuple[Bucket, ...]:
    """``data.buckets[]`` of an aggregate response, as (group values, count)."""
    buckets = _dict(_dict(body).get("data")).get("buckets")
    if not isinstance(buckets, list):
        return ()
    return tuple(
        Bucket(
            by={str(k): _str(v) for k, v in _dict(raw.get("by")).items()},
            count=_count(_dict(raw.get("computes")).get("c0")),
        )
        for raw in buckets
        if isinstance(raw, dict)
    )


def _tags(value: Any) -> tuple[str, ...]:
    return tuple(t for t in value if isinstance(t, str)) if isinstance(value, list) else ()


def _number(value: Any) -> float | None:
    """A JSON number as a float; None for anything else (``bool`` included)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _first(*values: Any) -> str:
    """The first non-empty string form, so a field may sit at any of its known homes."""
    for value in values:
        text = _str(value)
        if text:
            return text
    return ""


def _span(raw: dict[str, Any]) -> SpanRow:
    attrs = _dict(raw.get("attributes"))
    custom = _dict(attrs.get("custom"))
    inner = _dict(attrs.get("attributes"))
    duration = _int_or_none(custom.get("duration"))
    if duration is None:
        duration = _int_or_none(inner.get("duration"))
    return SpanRow(
        trace_id=_id_text(attrs.get("trace_id")),
        span_id=_id_text(attrs.get("span_id")),
        parent_id=_id_text(attrs.get("parent_id")),
        service=_str(attrs.get("service")),
        resource_name=_str(attrs.get("resource_name")),
        operation_name=_first(
            attrs.get("operation_name"), custom.get("operation_name"), inner.get("operation_name")
        ),
        start_timestamp=_str(attrs.get("start_timestamp")),
        end_timestamp=_str(attrs.get("end_timestamp")),
        duration_ns=duration,
        status=_first(attrs.get("status"), custom.get("status"), inner.get("status")),
        env=_str(attrs.get("env")),
        tags=_tags(attrs.get("tags")),
        attributes=custom,
    )


def parse_span_page(body: Any) -> SpanPage:
    """The spans and the next cursor (``meta.page.after``) of a span search response."""
    body = _dict(body)
    data = body.get("data")
    rows = (
        tuple(_span(item) for item in data if isinstance(item, dict))
        if isinstance(data, list)
        else ()
    )
    after = _dict(_dict(body.get("meta")).get("page")).get("after")
    return SpanPage(rows=rows, next_cursor=after if isinstance(after, str) and after else None)


def parse_span_buckets(body: Any) -> tuple[Bucket, ...]:
    """``data[].attributes`` of a span aggregate response (a list, unlike logs)."""
    data = _dict(body).get("data")
    if not isinstance(data, list):
        return ()
    buckets = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        attrs = _dict(raw.get("attributes"))
        compute = _dict(attrs.get("compute")) or _dict(attrs.get("computes"))
        buckets.append(
            Bucket(
                by={str(k): _str(v) for k, v in _dict(attrs.get("by")).items()},
                count=_count(compute.get("c0")),
            )
        )
    return tuple(buckets)


def parse_monitor_page(body: Any) -> MonitorPage:
    """``monitors[]`` and the ``metadata`` totals of a monitor search response."""
    body = _dict(body)
    monitors = body.get("monitors")
    rows = tuple(
        MonitorRow(
            id=_str(raw.get("id")),
            name=_str(raw.get("name")),
            status=_str(raw.get("status")),
            type=_str(raw.get("type")),
            tags=_tags(raw.get("tags")),
            last_triggered_ts=_int_or_none(raw.get("last_triggered_ts")),
        )
        for raw in (monitors if isinstance(monitors, list) else ())
        if isinstance(raw, dict)
    )
    meta = _dict(body.get("metadata"))
    return MonitorPage(
        rows=rows,
        total_count=_count(meta.get("total_count")),
        page=_count(meta.get("page")),
        page_count=_count(meta.get("page_count")),
    )


def _points(value: Any) -> tuple[tuple[int, float], ...]:
    points = []
    for point in value if isinstance(value, list) else ():
        if not isinstance(point, list) or len(point) < 2:
            continue
        ts, number = _number(point[0]), _number(point[1])
        if ts is not None and number is not None:
            points.append((int(ts), number))
    return tuple(points)


def parse_metric_series(body: Any) -> tuple[MetricSeries, ...]:
    """``series[]`` of a v1 metric query response; null points are skipped."""
    series = _dict(body).get("series")
    return tuple(
        MetricSeries(
            metric=_str(raw.get("metric")),
            scope=_str(raw.get("scope")),
            tags=_tags(raw.get("tag_set")),
            points=_points(raw.get("pointlist")),
        )
        for raw in (series if isinstance(series, list) else ())
        if isinstance(raw, dict)
    )
