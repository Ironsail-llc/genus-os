"""The organization's Logs page: its own lines from the shared Datadog account.

Several tenants share one Datadog account and one server-held, read-only key
(``robothor.integrations.datadog``). This router lets a tenant's owners,
admins and auditors search that tenant's logs and count its errors by service,
and nothing else:

* the tenant is the one the verified token acts in (``deps.get_tenant_id``),
  never a request parameter, so a root admin who switched into a child reads
  the child's scope and nobody can name another tenant;
* every query is composed by the client as ``(<tenant scope>) AND (<query>)``;
  a query that could change what that wrapping means is refused before
  anything is sent, and the refusal is a 400 carrying its plain reason, since
  it is the caller's to fix;
* the route never 500s. No keys, or no scope for this tenant, is
  ``not_configured`` with one plain sentence (the root operator at home is also
  told which half is missing); a Datadog failure is ``unavailable`` with its
  kind and nothing of the vendor's reply;
* credentials (the platform redactor, as on the journald source), email
  addresses and phone numbers in a row are masked before it leaves;
* each read writes one audit row: the redacted, truncated query, the window,
  the outcome and a count. Never a line's text.

The same rules hold for the page's other tabs: span search and single traces
(``/traces``; a trace outside the tenant's scope is the same 404 as one that
does not exist), monitors (``/monitors``), and the Datadog alerts and watch
findings Genus recorded for the tenant (``/signals``, read from the audit log
through ``robothor.audit.logger.query_log``). ``/overview`` is the root
operator's at home: per tenant it may reach, the error count, the firing
monitors and whether it is connected, each tenant on its own.

The key never reaches the browser: the dashboard calls these routes through its
proxy and Datadog sees only the bridge.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from robothor.integrations.datadog.client import DatadogError, client_from_settings
from robothor.integrations.datadog.scope import (
    NO_ACCESS_QUERY,
    ScopeError,
    compose,
    scope_for,
    trace_query,
)
from robothor.secrets.redaction import (
    PLACEHOLDER,
    redact,
    redact_personal_data,
    redact_tool_arguments,
)
from routers._audit import audited
from routers._operator import is_root, require_root_operator, require_tenant_audit_reader
from routers._params import ISO_TIMESTAMP, ISO_TIMESTAMP_PATTERN, iso_timestamp, positive_int

if TYPE_CHECKING:
    from robothor.integrations.datadog.client import DatadogClient
    from robothor.integrations.datadog.requests import LogRow, MonitorRow, SpanRow
    from robothor.integrations.datadog.scope import DatadogScope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

NOT_CONNECTED = "Datadog is not connected for this organization yet."

#: How much of the caller's query the audit row keeps. The row names what was
#: asked; it is not a copy of it.
AUDIT_QUERY_CHARS = 200

#: A relative age as the Observe pages write it (``lib/observe/since.ts``),
#: the same shape ``routers/logs.py`` takes for journald.
_RELATIVE_AGE = re.compile(r"(?P<count>\d{1,6})(?P<unit>[smhd])")
_TIME = re.compile(rf"(?:{_RELATIVE_AGE.pattern}|{ISO_TIMESTAMP_PATTERN})")

#: A Datadog facet: a name, optionally ``@``-prefixed, dotted. Nothing that
#: could be read as a second facet or as query syntax.
_FACET = re.compile(r"@?[A-Za-z_][A-Za-z0-9_.\-]*")

DEFAULT_SINCE = "1h"
DEFAULT_UNTIL = "now"


def _client() -> DatadogClient:
    """The configured client (the test seam). Raises ``DatadogError("not_configured")``."""
    return client_from_settings()


def _time(value: str | None, *, field: str, default: str) -> tuple[str, str]:
    """``(as the caller wrote it, as Datadog reads it)``, or a flat 422."""
    text = default if value is None or value == "" else value
    if text == "now":
        return text, "now"
    if not _TIME.fullmatch(text):
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be a relative age (30m, 1h, 7d) or an ISO-8601 timestamp",
        )
    relative = _RELATIVE_AGE.fullmatch(text)
    if relative:
        return text, f"now-{int(relative.group('count'))}{relative.group('unit')}"
    return text, iso_timestamp(text, field=field)


def _scrub(text: str) -> str:
    """``text`` with credentials, then email addresses and phone numbers, replaced.

    The credential pass is the one every other bridge surface that shows log
    or audit text runs (``routers/logs.py``, ``routers/audit.py``); a service
    that logs a bearer token must not hand it to an auditor here either.
    """
    return redact_personal_data(redact(text))


def _names_a_credential(name: str) -> bool:
    """Whether a key, tag name or facet (``@usr.password``) says it holds a credential.

    Asked of the platform's own per-argument redactor, so this route and the
    tool-call recorder agree on which names those are. It matters because
    Datadog parses ``password=… api_key=…`` into structured attributes: the
    value then arrives bare, with no shape of its own for :func:`redact` to
    find, and the name it sits under is the only thing left to match on.
    """
    key = name.lstrip("@")
    return bool(key) and redact_tool_arguments("", {key: "x"}).get(key) == PLACEHOLDER


def _masked(value: Any) -> Any:
    """``value`` with every string in it passed through :func:`_scrub`, and every
    value under a credential-named key replaced whatever its shape."""
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        return {
            str(k): PLACEHOLDER if _names_a_credential(str(k)) else _masked(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_masked(v) for v in value]
    return value


def _tag(tag: str) -> str:
    """A ``name:value`` tag, its value replaced when the name says credential."""
    name, sep, _value = tag.partition(":")
    if sep and _names_a_credential(name):
        return f"{name}:{PLACEHOLDER}"
    return _scrub(tag)


def _row_out(row: LogRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "service": row.service,
        "status": row.status,
        "host": row.host,
        "message": _scrub(row.message),
        "tags": [_tag(t) for t in row.tags],
        "attributes": _masked(row.attributes),
        # For linking a line to its trace; None when the event carries none.
        # The client passes on only an id-shaped value (hex or decimal digits),
        # wherever in the event it found it, so no free text arrives here.
        "trace_id": row.trace_id or None,
        "span_id": row.span_id or None,
    }


def _audited_query(query: str) -> str:
    return _scrub(query)[:AUDIT_QUERY_CHARS]


def _not_configured(request: Request, tenant_id: str, reason: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "not_configured",
        "message": NOT_CONNECTED,
        "scope": tenant_id,
    }
    if is_root(getattr(request.state, "auth", None)):
        # The root operator is who fixes it, so it is told which half to fix.
        body["reason"] = reason
    return body


def _audit(request: Request, event: str, tenant_id: str, outcome: str, **details: Any) -> None:
    audited(
        request,
        event,
        action=tenant_id,
        status="ok" if outcome == "ok" else "error",
        outcome=outcome,
        **details,
    )


def _read(
    request: Request,
    tenant_id: str,
    event: str,
    details: dict[str, Any],
    count_field: str,
    read: Any,
) -> tuple[str, Any]:
    """Run ``read(client, scope)`` under the state rules; audit it once.

    Returns ``("ok", result)`` or ``(<state>, <response body>)``. A refused
    query raises the 400 after its audit row is written.
    """
    scope = scope_for(tenant_id)
    if scope is None:
        _audit(request, event, tenant_id, "not_configured", **details, **{count_field: 0})
        return "not_configured", _not_configured(request, tenant_id, "scope")
    try:
        result = read(_client(), scope)
    except ScopeError as exc:
        _audit(request, event, tenant_id, "refused", **details, **{count_field: 0})
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except DatadogError as exc:
        if exc.kind == "not_configured":
            _audit(request, event, tenant_id, "not_configured", **details, **{count_field: 0})
            return "not_configured", _not_configured(request, tenant_id, "keys")
        logger.info("telemetry: datadog read failed for %s (%s)", tenant_id, exc.kind)
        _audit(request, event, tenant_id, "unavailable", **details, **{count_field: 0})
        return "unavailable", {"status": "unavailable", "kind": exc.kind, "scope": tenant_id}
    return "ok", result


@router.get("/logs")
def search_logs(
    request: Request,
    q: str = Query("", description="A Datadog log query, ANDed inside the tenant's scope"),
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
    until: str | None = Query(None, description="now (default), 30m / 1h / 7d, or ISO-8601"),
    limit: str = Query("50"),
    cursor: str | None = Query(None),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_tenant_audit_reader(request)
    since_label, start = _time(since, field="since", default=DEFAULT_SINCE)
    until_label, end = _time(until, field="until", default=DEFAULT_UNTIL)
    page_size = positive_int(limit, field="limit")
    details = {"query": _audited_query(q), "since": since_label, "until": until_label}

    def read(client: DatadogClient, scope: Any) -> Any:
        return client.search_logs(
            scope, q, start=start, end=end, limit=page_size, cursor=cursor or None
        )

    state, result = _read(request, tenant_id, "telemetry.logs.read", details, "rows", read)
    if state != "ok":
        return dict(result)
    _audit(request, "telemetry.logs.read", tenant_id, "ok", **details, rows=len(result.rows))
    return {
        "status": "ok",
        "rows": [_row_out(r) for r in result.rows],
        "next_cursor": result.next_cursor,
        "scope": tenant_id,
    }


@router.get("/logs/summary")
def summarize_logs(
    request: Request,
    q: str = Query("", description="A Datadog log query, ANDed inside the tenant's scope"),
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
    until: str | None = Query(None, description="now (default), 30m / 1h / 7d, or ISO-8601"),
    group_by: str = Query("service", description="The facet to count by"),
    errors: bool = Query(True, description="Count only status:error lines (the default)"),
    limit: str = Query("25"),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_tenant_audit_reader(request)
    since_label, start = _time(since, field="since", default=DEFAULT_SINCE)
    until_label, end = _time(until, field="until", default=DEFAULT_UNTIL)
    if not _FACET.fullmatch(group_by):
        raise HTTPException(status_code=422, detail="group_by must be one facet name")
    top = positive_int(limit, field="limit")
    details = {
        "query": _audited_query(q),
        "since": since_label,
        "until": until_label,
        "group_by": group_by,
        "errors": errors,
    }

    def read(client: DatadogClient, scope: Any) -> Any:
        query = q.strip()
        if errors:
            # The caller's query is checked on its own first, so it cannot
            # close the error filter's parenthesis any more than the scope's.
            compose("*", query)
            query = f"status:error AND ({query})" if query else "status:error"
        return client.aggregate_logs(
            scope, query, start=start, end=end, group_by=[group_by], limit=top
        )

    state, result = _read(request, tenant_id, "telemetry.logs.summary", details, "buckets", read)
    if state != "ok":
        return dict(result)
    _audit(request, "telemetry.logs.summary", tenant_id, "ok", **details, buckets=len(result))
    # Grouped by a credential-named facet, every bucket value is a credential.
    hidden = _names_a_credential(group_by)
    return {
        "status": "ok",
        "buckets": [
            {"by": PLACEHOLDER if hidden else _scrub(b.by.get(group_by, "")), "count": b.count}
            for b in result
        ],
        "group_by": group_by,
        "errors": errors,
        "scope": tenant_id,
    }


# ── traces ──────────────────────────────────────────────────────────────────

#: What a trace that is not this tenant's reads as. The same sentence as for a
#: trace that does not exist or an id that is malformed, so the answer says
#: nothing about other tenants. Datadog keeps only a sample of traces, so a
#: trace id from an ordinary log line often has no trace; the sentence says so.
TRACE_NOT_FOUND = (
    "This trace was not found in your organization's traces. Datadog keeps only a "
    "sample of traces, so most ordinary requests have none."
)

#: The window a single trace is looked for in when the caller names none: the
#: longest range the page offers, so a trace opened from any row is in it.
DEFAULT_TRACE_SINCE = "7d"

#: One trace is read in one span search of Datadog's largest page.
TRACE_SPANS = 1000


def _span_row_out(row: SpanRow) -> dict[str, Any]:
    return {
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "service": _scrub(row.service),
        "resource": _scrub(row.resource_name),
        "operation": _scrub(row.operation_name),
        "timestamp": row.start_timestamp,
        "duration_ns": row.duration_ns,
        "status": row.status,
    }


def _span_out(row: SpanRow) -> dict[str, Any]:
    return {
        **_span_row_out(row),
        "parent_id": row.parent_id,
        "end": row.end_timestamp,
        "env": row.env,
        "tags": [_tag(t) for t in row.tags],
    }


TRACES_NOT_SHARED = "Traces are not shared with this organization."


def _shared(filter_query: str) -> bool:
    """Whether a scope part grants anything: span and monitor access are opt-in."""
    return filter_query.strip() not in ("", NO_ACCESS_QUERY)


def _not_shared(
    request: Request,
    tenant_id: str,
    event: str,
    part: str,
    message: str,
    details: dict[str, Any],
    count_field: str,
) -> dict[str, Any] | None:
    """The ``no_access`` answer (audited) when this tenant's scope shares no ``part``."""
    scope = scope_for(tenant_id)
    if scope is None or _shared(getattr(scope, part)):
        return None
    _audit(request, event, tenant_id, "no_access", **details, **{count_field: 0})
    return {"status": "no_access", "message": message, "scope": tenant_id}


@router.get("/traces")
def search_traces(
    request: Request,
    q: str = Query("status:error", description="A Datadog span query; errors by default"),
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
    until: str | None = Query(None, description="now (default), 30m / 1h / 7d, or ISO-8601"),
    limit: str = Query("50"),
    cursor: str | None = Query(None),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_tenant_audit_reader(request)
    since_label, start = _time(since, field="since", default=DEFAULT_SINCE)
    until_label, end = _time(until, field="until", default=DEFAULT_UNTIL)
    page_size = positive_int(limit, field="limit")
    details = {"query": _audited_query(q), "since": since_label, "until": until_label}
    refused = _not_shared(
        request, tenant_id, "telemetry.traces.read", "spans", TRACES_NOT_SHARED, details, "rows"
    )
    if refused is not None:
        return refused

    def read(client: DatadogClient, scope: Any) -> Any:
        return client.search_spans(
            scope, q, start=start, end=end, limit=page_size, cursor=cursor or None
        )

    state, result = _read(request, tenant_id, "telemetry.traces.read", details, "rows", read)
    if state != "ok":
        return dict(result)
    _audit(request, "telemetry.traces.read", tenant_id, "ok", **details, rows=len(result.rows))
    return {
        "status": "ok",
        "rows": [_span_row_out(r) for r in result.rows],
        "next_cursor": result.next_cursor,
        "scope": tenant_id,
    }


@router.get("/traces/{trace_id}")
def get_trace(
    request: Request,
    trace_id: str,
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
    until: str | None = Query(None, description="now (default), 30m / 1h / 7d, or ISO-8601"),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_tenant_audit_reader(request)
    since_label, start = _time(since, field="since", default=DEFAULT_TRACE_SINCE)
    until_label, end = _time(until, field="until", default=DEFAULT_UNTIL)
    event = "telemetry.trace.read"
    window = {"since": since_label, "until": until_label}
    refused = _not_shared(request, tenant_id, event, "spans", TRACES_NOT_SHARED, window, "spans")
    if refused is not None:
        return refused
    try:
        # An id that is not plain hex or decimal digits names no trace anywhere,
        # so it gets the not-found answer, not a refusal that tells it apart.
        trace_query(trace_id)
    except ScopeError:
        _audit(request, event, tenant_id, "not_found", trace_id="", spans=0)
        raise HTTPException(status_code=404, detail=TRACE_NOT_FOUND) from None
    details = {"trace_id": trace_id.strip(" "), "since": since_label, "until": until_label}

    def read(client: DatadogClient, scope: Any) -> Any:
        return client.get_trace(scope, trace_id, start=start, end=end, limit=TRACE_SPANS)

    state, spans = _read(request, tenant_id, event, details, "spans", read)
    if state != "ok":
        return dict(spans)
    if not spans:
        _audit(request, event, tenant_id, "not_found", **details, spans=0)
        raise HTTPException(status_code=404, detail=TRACE_NOT_FOUND)
    _audit(request, event, tenant_id, "ok", **details, spans=len(spans))
    ordered = sorted(spans, key=lambda s: (s.start_timestamp, s.span_id))
    return {
        "status": "ok",
        "trace_id": details["trace_id"],
        "spans": [_span_out(s) for s in ordered],
        "scope": tenant_id,
    }


# ── monitors ────────────────────────────────────────────────────────────────

MONITORS_NOT_SHARED = "Monitors are not shared with this organization."

#: Monitor statuses that mean someone should look (Datadog spells them so).
FIRING_STATUSES = frozenset({"alert", "warn", "no data"})

#: Firing, asked of Datadog's monitor search: those statuses, and not muted. The
#: watch counts firing monitors with the same filter, so the page and the
#: briefs agree; a muted monitor in alert is someone's decision, not news.
FIRING_FILTER = '(status:alert OR status:warn OR status:"no data") AND muted:false'


def _firing_query(query: str) -> str:
    """``query`` narrowed to firing monitors; raises ScopeError for a refused query.

    The caller's query is checked on its own first, so it cannot close the
    filter's parenthesis any more than the scope's.
    """
    text = query.strip()
    compose("*", text)
    return f"{FIRING_FILTER} AND ({text})" if text else FIRING_FILTER


#: Monitors per search page; the route reads every page.
MONITOR_PAGE = 100


def _is_firing(status: str) -> bool:
    return status.strip().lower() in FIRING_STATUSES


def _monitors_shared(scope: DatadogScope) -> bool:
    return _shared(scope.monitors)


def _all_monitors(client: DatadogClient, scope: DatadogScope, query: str) -> list[MonitorRow]:
    """Every monitor inside the scope that matches ``query``, page by page."""
    rows: list[MonitorRow] = []
    page = 0
    while True:
        result = client.search_monitors(scope, query, limit=MONITOR_PAGE, page=page)
        rows.extend(result.rows)
        page += 1
        if not result.rows or page >= result.page_count:
            return rows


def _epoch_iso(value: int | None) -> str | None:
    if value is None or value <= 0:
        return None
    # Datadog writes this field in milliseconds; a seconds value reads the same.
    seconds = value / 1000 if value > 100_000_000_000 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _monitor_out(row: MonitorRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": _scrub(row.name),
        "status": row.status,
        "firing": _is_firing(row.status),
        "type": row.type,
        "last_triggered": _epoch_iso(row.last_triggered_ts),
    }


@router.get("/monitors")
def search_monitors(
    request: Request,
    q: str = Query("", description="A Datadog monitor search, ANDed inside the tenant's scope"),
    firing: bool = Query(False, description="Only monitors in alert, warn or no data"),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_tenant_audit_reader(request)
    event = "telemetry.monitors.read"
    details = {"query": _audited_query(q), "firing": firing}
    refused = _not_shared(
        request, tenant_id, event, "monitors", MONITORS_NOT_SHARED, details, "monitors"
    )
    if refused is not None:
        return refused

    def read(client: DatadogClient, scope: Any) -> Any:
        return _all_monitors(client, scope, _firing_query(q) if firing else q)

    state, rows = _read(request, tenant_id, event, details, "monitors", read)
    if state != "ok":
        return dict(rows)
    _audit(request, event, tenant_id, "ok", **details, monitors=len(rows))
    return {
        "status": "ok",
        "monitors": [_monitor_out(r) for r in rows],
        "firing": firing,
        "scope": tenant_id,
    }


# ── recorded activity ───────────────────────────────────────────────────────

#: The audit event types the Datadog recorders write, by the page's kind.
SIGNAL_EVENTS = {
    "alerts": "datadog.alert_received",
    "watch": "datadog.watch_finding",
}
DEFAULT_SIGNALS_SINCE = "7d"
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _since_timestamp(value: str | None, default: str) -> tuple[str, str]:
    """``(as written, as an ISO timestamp)`` for an audit-log lower bound, or a 422."""
    text = default if value is None or value == "" else value
    relative = _RELATIVE_AGE.fullmatch(text)
    if relative:
        seconds = int(relative.group("count")) * _UNIT_SECONDS[relative.group("unit")]
        return text, (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    if not ISO_TIMESTAMP.fullmatch(text):
        raise HTTPException(
            status_code=422,
            detail="since must be a relative age (30m, 1h, 7d) or an ISO-8601 timestamp",
        )
    return text, iso_timestamp(text, field="since")


def _text(value: Any) -> str:
    return _scrub(value) if isinstance(value, str) else ""


def _dict_of(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_link(value: Any) -> str | None:
    """Only an https link with no credentials in it is passed on."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or "@" in parsed.netloc:
        return None
    return value.strip()


def _recipients(would_notify: Any) -> str:
    """Who would have been told, in words. The recorders write a dict or a sentence."""
    if isinstance(would_notify, str):
        return _text(would_notify)
    notify = _dict_of(would_notify)
    if not notify:
        return ""
    audience = _text(notify.get("audience")) or "the instance operator"
    channels = [_text(c) for c in notify.get("channels") or () if isinstance(c, str)]
    if not channels:
        return audience
    joined = channels[0] if len(channels) == 1 else f"{', '.join(channels[:-1])} and {channels[-1]}"
    return f"{audience}, by {joined}"


#: What a recorded record's ``mode`` says happened to it. Older records are
#: ``dry_run``; a recorder that delivers writes ``delivered``, ``suppressed`` or
#: ``delivery_failed`` with a ``reason``. Anything else reads as not sent.
DELIVERED = "delivered"


def _delivery_route(delivery: Any) -> str | None:
    """The route a delivered record went by, from whatever shape ``delivery`` has."""
    if isinstance(delivery, str):
        return _text(delivery) or None
    record = _dict_of(delivery)
    for key in ("route", "channel"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return _text(value.strip())
    return None


def _triage(triage: Any) -> dict[str, Any] | None:
    """Whether the read-only first-look run was started, and why not, from any shape."""
    record = _dict_of(triage)
    if not record:
        return None
    started = record.get("started")
    if not isinstance(started, bool):
        started = bool(record.get("run_id"))
    reason = record.get("reason")
    return {"started": started, "reason": _text(reason) or None}


def _signal_base(row: dict[str, Any], kind: str, details: dict[str, Any]) -> dict[str, Any]:
    mode = _text(details.get("mode")) or "dry_run"
    return {
        "id": row.get("id"),
        "type": kind,
        "timestamp": row.get("timestamp") or "",
        "mode": mode,
        "sent": mode == DELIVERED,
        "reason": _text(details.get("reason")) or None,
        "recipients": _recipients(details.get("would_notify")),
        "delivery_route": _delivery_route(details.get("delivery")),
    }


def _alert_out(row: dict[str, Any]) -> dict[str, Any]:
    details = _dict_of(row.get("details"))
    run = _dict_of(details.get("would_run"))
    return {
        **_signal_base(row, "alert", details),
        "title": _text(details.get("title")),
        "transition": _text(details.get("transition")),
        "priority": _text(details.get("priority")),
        "monitor_id": _text(details.get("monitor_id")),
        "link": _safe_link(details.get("link")),
        "level": _text(_dict_of(details.get("would_notify")).get("level")),
        "triage": _triage(details.get("triage")),
        "would_run": (
            {"agent": _text(run.get("agent")) or None, "prompt": _text(run.get("prompt"))}
            if run
            else None
        ),
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _watch_out(row: dict[str, Any]) -> dict[str, Any]:
    details = _dict_of(row.get("details"))
    window = _dict_of(details.get("window"))
    return {
        **_signal_base(row, "watch", details),
        "finding": _text(details.get("kind")),
        "service": _text(details.get("service")) or None,
        "count": _int_or_none(details.get("current")),
        "window": (
            {
                "start": _text(window.get("start")),
                "end": _text(window.get("end")),
                "minutes": _int_or_none(window.get("minutes")),
            }
            if window
            else None
        ),
        "message": _text(details.get("brief")),
    }


_SIGNAL_SHAPES = {"alerts": _alert_out, "watch": _watch_out}


@router.get("/signals")
def list_signals(
    request: Request,
    kind: str | None = Query(None, description="alerts or watch; both when omitted"),
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
    limit: str = Query("50"),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """What the Datadog recorders wrote for this tenant: alerts received, watch findings."""
    from robothor.audit import logger as audit_log

    require_tenant_audit_reader(request)
    if kind is not None and kind not in SIGNAL_EVENTS:
        raise HTTPException(status_code=422, detail="kind must be alerts or watch")
    since_label, lower = _since_timestamp(since, DEFAULT_SIGNALS_SINCE)
    top = positive_int(limit, field="limit")
    signals: list[dict[str, Any]] = []
    for name in [kind] if kind else list(SIGNAL_EVENTS):
        rows = audit_log.query_log(
            limit=top, event_type=SIGNAL_EVENTS[name], since=lower, tenants=[tenant_id]
        )
        signals.extend(_SIGNAL_SHAPES[name](r) for r in rows if isinstance(r, dict))
    signals.sort(key=lambda s: str(s["timestamp"]), reverse=True)
    signals = signals[:top]
    _audit(
        request,
        "telemetry.signals.read",
        tenant_id,
        "ok",
        kind=kind or "all",
        since=since_label,
        signals=len(signals),
    )
    return {"status": "ok", "signals": signals, "scope": tenant_id}


# ── the root's overview ─────────────────────────────────────────────────────

#: Tenants read at once for the overview. A pool size, not a limit: every
#: accessible tenant is read, a few at a time.
OVERVIEW_WORKERS = 4


def _kind_of(exc: Exception) -> str:
    if isinstance(exc, DatadogError):
        return str(exc.kind)
    if isinstance(exc, ScopeError):
        return "bad_query"
    return "unknown"


def _tenant_figures(client: DatadogClient, tenant_id: str, start: str) -> dict[str, Any]:
    """One tenant's error count and firing monitors; every failure becomes a kind."""
    scope = scope_for(tenant_id)
    entry: dict[str, Any] = {
        "tenant_id": tenant_id,
        "connected": scope is not None,
        "errors": None,
        "firing_monitors": None,
        "error_kind": None,
    }
    if scope is None:
        return entry
    try:
        buckets = client.aggregate_logs(scope, "status:error", start=start, end="now", group_by=[])
        entry["errors"] = sum(b.count for b in buckets)
    except Exception as exc:  # noqa: BLE001 — one tenant's failure is its own row
        entry["error_kind"] = _kind_of(exc)
    if _monitors_shared(scope):
        try:
            entry["firing_monitors"] = len(_all_monitors(client, scope, FIRING_FILTER))
        except Exception as exc:  # noqa: BLE001 — one tenant's failure is its own row
            entry["error_kind"] = entry["error_kind"] or _kind_of(exc)
    if entry["error_kind"]:
        logger.info("telemetry: overview read failed for %s (%s)", tenant_id, entry["error_kind"])
    return entry


def _overview_audit(
    request: Request, home: str, target: str, outcome: str, since: str, **counts: Any
) -> None:
    """One row per tenant read, filed under that tenant so its admins see who looked."""
    audited(
        request,
        "telemetry.overview",
        action=target,
        status="ok" if outcome == "ok" else "error",
        audit_tenant_id=target,
        user_id=request.state.auth.user_id,
        home_tenant_id=home,
        target_tenant_id=target,
        outcome=outcome,
        since=since,
        **counts,
    )


@router.get("/overview")
def overview(
    request: Request,
    since: str | None = Query(None, description="30m / 1h / 7d, or an ISO-8601 timestamp"),
) -> dict[str, Any]:
    """Per organization the root may reach: errors in the window, firing monitors, connected.

    The root owner/admin AT HOME only (the ``GET /api/org/overview`` pattern);
    the tenants come from the server-side hierarchy, never from the request. A
    plain ``def``, so FastAPI runs it in its threadpool; the per-tenant reads
    run a few at a time in a small pool of their own.
    """
    from robothor.engine.permissions import resolve_accessible_tenants

    require_root_operator(request)
    auth = request.state.auth
    home = auth.home_tenant_id
    since_label, start = _time(since, field="since", default=DEFAULT_SINCE)
    tenants = list(resolve_accessible_tenants(home, auth.role))
    try:
        client = _client()
    except DatadogError as exc:
        for target in tenants:
            _overview_audit(request, home, target, "not_configured", since_label)
        if exc.kind != "not_configured":
            return {"status": "unavailable", "kind": exc.kind, "scope": home}
        return _not_configured(request, home, "keys")
    with ThreadPoolExecutor(max_workers=max(1, min(OVERVIEW_WORKERS, len(tenants)))) as pool:
        entries = list(pool.map(lambda t: _tenant_figures(client, t, start), tenants))
    for entry in entries:
        if not entry["connected"]:
            outcome = "not_configured"
        else:
            outcome = "unavailable" if entry["error_kind"] else "ok"
        _overview_audit(
            request,
            home,
            entry["tenant_id"],
            outcome,
            since_label,
            errors=entry["errors"] or 0,
            firing_monitors=entry["firing_monitors"] or 0,
        )
    return {"status": "ok", "since": since_label, "home_tenant_id": home, "tenants": entries}
