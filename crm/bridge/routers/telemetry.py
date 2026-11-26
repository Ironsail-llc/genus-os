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

The key never reaches the browser: the dashboard calls these routes through its
proxy and Datadog sees only the bridge.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from robothor.integrations.datadog.client import DatadogError, client_from_settings
from robothor.integrations.datadog.scope import ScopeError, compose, scope_for
from robothor.secrets.redaction import (
    PLACEHOLDER,
    redact,
    redact_personal_data,
    redact_tool_arguments,
)
from routers._audit import audited
from routers._operator import is_root, require_tenant_audit_reader
from routers._params import ISO_TIMESTAMP_PATTERN, iso_timestamp, positive_int

if TYPE_CHECKING:
    from robothor.integrations.datadog.client import DatadogClient
    from robothor.integrations.datadog.requests import LogRow

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
