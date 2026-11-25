"""Datadog API requests and responses as plain data: builders and parsers, no I/O.

The clients in :mod:`.client` are thin shells over these. Parsers are
defensive: a missing or oddly typed field becomes an empty value, never a
``KeyError``, because a vendor response is input, not a contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    "api_base",
    "logs_aggregate",
    "logs_search",
    "parse_buckets",
    "parse_log_page",
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


@dataclass(frozen=True)
class LogPage:
    rows: tuple[LogRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class Bucket:
    by: dict[str, str]
    count: int


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
    page: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_PAGE))}
    if cursor:
        page["cursor"] = cursor
    body = {
        "filter": _filter(query, start, end),
        "sort": "-timestamp" if newest_first else "timestamp",
        "page": page,
    }
    return ApiRequest("POST", "/api/v2/logs/events/search", body)


def logs_aggregate(
    query: str, *, start: str, end: str, group_by: Sequence[str], limit: int = 25
) -> ApiRequest:
    """``POST /api/v2/logs/analytics/aggregate``: a count, grouped by each facet."""
    body: dict[str, Any] = {
        "filter": _filter(query, start, end),
        "compute": [{"aggregation": "count"}],
    }
    if group_by:
        sort = {"aggregation": "count", "order": "desc", "type": "measure"}
        body["group_by"] = [
            {"facet": facet, "limit": max(1, int(limit)), "sort": dict(sort)} for facet in group_by
        ]
    return ApiRequest("POST", "/api/v2/logs/analytics/aggregate", body)


def validate_request() -> ApiRequest:
    """``GET /api/v1/validate``: checks the API key (Datadog ignores the application key)."""
    return ApiRequest("GET", "/api/v1/validate")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return "" if value is None or isinstance(value, dict | list) else str(value)


def _row(raw: dict[str, Any]) -> LogRow:
    attrs = _dict(raw.get("attributes"))
    tags = attrs.get("tags")
    return LogRow(
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
