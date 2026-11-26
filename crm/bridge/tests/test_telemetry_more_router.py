"""The Logs page's other tabs: traces, monitors, recorded activity, and the root's overview.

What this file pins, on top of ``test_telemetry_router.py`` (same router,
same rules):

* ``/traces`` searches the tenant's spans (errors by default) and
  ``/traces/{id}`` returns one trace's spans in order with their parents; a
  trace outside the tenant's ``spans`` filter is a 404 whose body is the same
  sentence as for a trace that does not exist;
* ``/monitors`` lists the monitors inside the tenant's ``monitors`` filter,
  every page of them, and ``firing=true`` keeps alert, warn and no data;
* ``/signals`` reads the recorded Datadog alerts and watch findings of the
  token's tenant through ``robothor.audit.logger.query_log`` and returns only
  what the page shows, masked;
* ``/overview`` is the root owner/admin AT HOME only, takes the tenant list
  from the server-side hierarchy, and reports each tenant on its own: one
  tenant failing never fails the response;
* every read writes one audit row with counts and no content, and nothing
  500s.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

SCOPES = {
    ROOT: {"logs": "*", "spans": "*", "monitors": "*"},
    CHILD_A: {
        "logs": "service:shop-api",
        "spans": "service:shop-api",
        "monitors": 'tag:"team:shop"',
    },
    CHILD_B: {
        "logs": "service:clinic-app",
        "spans": "service:clinic-app",
        "monitors": 'tag:"team:clinic"',
    },
}

NOT_CONNECTED = "Datadog is not connected for this organization yet."
TRACE_ID = "abc123def4567890"


def _span(span_id: str, parent: str, start: str, *, status: str = "ok", **extra: Any) -> dict:
    return {
        "id": f"ev-{span_id}",
        "attributes": {
            "trace_id": TRACE_ID,
            "span_id": span_id,
            "parent_id": parent,
            "service": extra.get("service", "shop-api"),
            "resource_name": extra.get("resource", "POST /checkout"),
            "operation_name": "http.request",
            "start_timestamp": start,
            "end_timestamp": start,
            "status": status,
            "env": "prod",
            "tags": extra.get("tags", ["env:prod"]),
            "custom": {"duration": extra.get("duration", 1_500_000)},
        },
    }


SPANS = {
    "data": [
        _span("5a1", "0", "2026-11-25T10:00:00Z", status="error"),
    ],
    "meta": {"page": {"after": "span-cursor-2"}},
}

# Deliberately out of order: the route orders by start time.
TRACE = {
    "data": [
        _span("12cc", "10aa", "2026-11-25T10:00:02Z", resource="SELECT orders"),
        _span("10aa", "0", "2026-11-25T10:00:00Z", status="error", duration=9_000_000),
        _span("11bb", "10aa", "2026-11-25T10:00:01Z", resource="GET /cart"),
    ]
}


def _monitor(mid: int, name: str, status: str, ts: int | None = 1764064800000) -> dict:
    return {
        "id": mid,
        "name": name,
        "status": status,
        "type": "log alert",
        "tags": ["team:shop"],
        "last_triggered_ts": ts,
    }


MONITOR_PAGES = [
    {
        "monitors": [
            _monitor(1, "Checkout errors", "Alert"),
            _monitor(2, "Cart latency", "OK", None),
        ],
        "metadata": {"page": 0, "page_count": 2, "total_count": 4, "per_page": 2},
    },
    {
        "monitors": [
            _monitor(3, "Payments no data", "No Data"),
            _monitor(4, "Queue depth", "Warn"),
            # Muted while it alerts: not firing, as the watch counts it.
            {**_monitor(5, "Muted checkout", "Alert"), "overall_state": "Alert", "muted": True},
        ],
        "metadata": {"page": 1, "page_count": 2, "total_count": 4, "per_page": 2},
    },
]


#: Firing, as Datadog's monitor search spells it and the watch counts it.
FIRING_FILTER = '(status:alert OR status:warn OR status:"no data") AND muted:false'


class FakeDatadog:
    """Records every request the real client sends, and answers it by path."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.spans: dict[str, Any] = SPANS
        self.trace: dict[str, Any] = TRACE
        self.monitor_pages: list[dict[str, Any]] = MONITOR_PAGES
        self.error_counts: dict[str, int] = {"*": 12, "service:shop-api": 5}
        #: Scope filters whose calls fail with this HTTP status.
        self.failing: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        params = {
            k: v[0] for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()
        }
        self.requests.append({"path": request.url.path, "body": body, "params": params})
        query = params.get("query") or _filter_query(body)
        for needle, status in self.failing.items():
            if needle in query:
                return httpx.Response(status, json={"errors": ["vendor text"]})
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["vendor text"]})
        path = request.url.path
        if path == "/api/v1/monitor/search":
            page = int(params.get("page", "0"))
            if page < len(self.monitor_pages):
                body = self.monitor_pages[page]
                if FIRING_FILTER in query:
                    # What Datadog does with the status and muted filter.
                    kept = [
                        m
                        for m in body["monitors"]
                        if str(m["status"]).lower() in ("alert", "warn", "no data")
                        and not m.get("muted")
                    ]
                    body = {**body, "monitors": kept}
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"monitors": [], "metadata": {"page_count": 0}})
        if path.endswith("/spans/events/search"):
            return httpx.Response(200, json=self.trace if "trace_id:" in query else self.spans)
        if path.endswith("/logs/analytics/aggregate"):
            count = next(
                (n for scope, n in self.error_counts.items() if scope in query),
                self.error_counts.get("*", 0),
            )
            return httpx.Response(
                200, json={"data": {"buckets": [{"by": {}, "computes": {"c0": count}}]}}
            )
        return httpx.Response(404, json={})

    def client(self):
        from robothor.integrations.datadog.client import DatadogClient

        return DatadogClient("api-key", "app-key", transport=httpx.MockTransport(self.handler))

    def sent(self, suffix: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith(suffix)]


def _filter_query(body: dict[str, Any]) -> str:
    attrs = body.get("data", {}).get("attributes", body) if isinstance(body, dict) else {}
    return str((attrs.get("filter") or {}).get("query", ""))


def _accessible(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {ROOT: (ROOT, CHILD_A, CHILD_B), CHILD_A: (CHILD_A,), CHILD_B: (CHILD_B,)}[tenant]


# ─── recorded activity, as the sibling recorders write it ───────────────


def _alert_event(tenant: str = CHILD_A, **over: Any) -> dict[str, Any]:
    details = {
        "monitor_id": "1234",
        "event_id": "ev-1",
        "title": "Checkout errors for alice@example.com",
        "transition": "triggered",
        "datadog_transition": "Triggered",
        "status": "Alert",
        "priority": "P2",
        "tags": ["org:acme-child-a", "owner:bob@example.com"],
        "link": "https://app.example.com/monitors/1234",
        "date": "2026-11-25T10:00:00Z",
        "routing": "tag",
        "mode": "dry_run",
        "would_notify": {
            "level": "critical",
            "route": "page",
            "channels": ["telegram", "email"],
            "audience": "the instance operator",
        },
        "would_run": {
            "tenant": tenant,
            "agent": "main",
            "read_only": True,
            "prompt": "Datadog monitor 1234 (Checkout errors) changed to triggered. Change nothing.",
        },
        **over,
    }
    return {
        "id": 11,
        "timestamp": "2026-11-25T10:00:05+00:00",
        "event_type": "datadog.alert_received",
        "category": "datadog",
        "actor": "datadog",
        "action": "datadog monitor 1234 triggered",
        "details": details,
        "source_channel": "webhook",
        "target": "datadog_monitor:1234",
        "status": "ok",
        "session_key": None,
        "user_id": "",
    }


def _watch_event(**over: Any) -> dict[str, Any]:
    details = {
        "kind": "error_spike",
        "service": "shop-api",
        "current": 40,
        "baseline": {"median": 3, "spread": 1.2, "windows": 8, "new_service": False},
        "window": {
            "start": "2026-11-25T09:45:00+00:00",
            "end": "2026-11-25T10:00:00+00:00",
            "minutes": 15,
        },
        "mode": "dry_run",
        "would_notify": "main agent alert digest, tenant acme-child-a",
        "brief": "shop-api logged 40 errors in the 15 minutes to 10:00 UTC. Ask bob@example.com.",
        **over,
    }
    return {
        "id": 12,
        "timestamp": "2026-11-25T10:01:00+00:00",
        "event_type": "datadog.watch_finding",
        "category": "datadog",
        "actor": "engine",
        "action": "error_spike",
        "details": details,
        "source_channel": None,
        "target": "shop-api",
        "status": "ok",
        "session_key": None,
        "user_id": "",
    }


class FakeAuditLog:
    """Stands in for ``robothor.audit.logger.query_log`` and records the calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.by_type: dict[str, list[dict[str, Any]]] = {
            "datadog.alert_received": [_alert_event()],
            "datadog.watch_finding": [_watch_event()],
        }

    def __call__(self, limit: int = 50, event_type: str | None = None, **kw: Any):
        self.calls.append({"limit": limit, "event_type": event_type, **kw})
        return list(self.by_type.get(event_type or "", []))[:limit]


@pytest.fixture
def datadog():
    return FakeDatadog()


@pytest.fixture
def audit_log():
    return FakeAuditLog()


@pytest.fixture
def audit_rows():
    return []


@pytest.fixture(autouse=True)
def _env(monkeypatch, datadog, audit_rows, audit_log):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps(SCOPES))
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    reset_settings()

    def _log_event(event_type, **kw):
        audit_rows.append({"event_type": event_type, **kw})

    with (
        patch("routers.telemetry._client", side_effect=datadog.client),
        patch("routers._audit.log_event", side_effect=_log_event),
        patch("robothor.audit.logger.query_log", new=audit_log),
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_accessible),
    ):
        yield
    tokens.reset_signing_key_cache()
    reset_settings()


def _client(tenant: str, role: str = "admin", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", tenant, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _service_client(tenant: str):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_service_token("svc-agent", tenant)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _ok(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok", body
    return body


def _telemetry_rows(audit_rows, event: str | None = None):
    rows = [r for r in audit_rows if r["event_type"].startswith("telemetry.")]
    return [r for r in rows if r["event_type"] == event] if event else rows


TENANT_ROUTES = [
    "/api/telemetry/traces",
    f"/api/telemetry/traces/{TRACE_ID}",
    "/api/telemetry/monitors",
    "/api/telemetry/signals",
]


# ─── who may read ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", TENANT_ROUTES)
@pytest.mark.parametrize("role", ["owner", "admin", "auditor"])
def test_owners_admins_and_auditors_read(path, role):
    _ok(_client(CHILD_A, role).get(path))


@pytest.mark.parametrize("path", [*TENANT_ROUTES, "/api/telemetry/overview"])
@pytest.mark.parametrize("role", ["viewer", "member", "user"])
def test_any_other_role_is_refused(path, role, datadog, audit_rows, audit_log):
    response = _client(CHILD_A, role).get(path)

    assert response.status_code == 403
    assert datadog.requests == []
    assert audit_log.calls == []
    assert _telemetry_rows(audit_rows) == []


@pytest.mark.parametrize("path", TENANT_ROUTES)
def test_a_tenant_named_in_the_request_changes_nothing(path, datadog, audit_log):
    body = _ok(_client(CHILD_A).get(path, params={"tenant_id": CHILD_B, "tenant": CHILD_B}))

    assert body["scope"] == CHILD_A
    sent = json.dumps(datadog.requests) + json.dumps(audit_log.calls)
    assert "clinic" not in sent
    assert CHILD_B not in sent


# ─── traces ────────────────────────────────────────────────────────────


def test_traces_default_to_the_tenants_errors(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/traces"))

    sent = datadog.sent("/spans/events/search")[-1]["body"]["data"]["attributes"]
    # The span search is bounded by the tenant's own ``spans`` filter.
    assert sent["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert sent["filter"]["from"] == "now-1h"
    row = body["rows"][0]
    assert row == {
        "trace_id": TRACE_ID,
        "span_id": "5a1",
        "service": "shop-api",
        "resource": "POST /checkout",
        "operation": "http.request",
        "timestamp": "2026-11-25T10:00:00Z",
        "duration_ns": 1_500_000,
        "status": "error",
    }
    assert body["next_cursor"] == "span-cursor-2"
    assert body["scope"] == CHILD_A


def test_traces_take_a_query_a_window_and_a_cursor(datadog):
    _ok(
        _client(CHILD_A).get(
            "/api/telemetry/traces",
            params={
                "q": "service:shop-api env:prod",
                "since": "24h",
                "cursor": "c1",
                "limit": "20",
            },
        )
    )

    sent = datadog.sent("/spans/events/search")[-1]["body"]["data"]["attributes"]
    assert sent["filter"]["query"] == "(service:shop-api) AND (service:shop-api env:prod)"
    assert sent["filter"]["from"] == "now-24h"
    assert sent["page"] == {"limit": 20, "cursor": "c1"}


def test_a_refused_trace_query_is_a_400(datadog):
    response = _client(CHILD_A).get("/api/telemetry/traces", params={"q": "a) OR (b"})

    assert response.status_code == 400
    assert response.json() == {"detail": "the brackets or parentheses close out of order"}
    assert datadog.requests == []


def test_a_trace_is_its_spans_in_order_with_their_parents(datadog):
    body = _ok(_client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}"))

    sent = datadog.sent("/spans/events/search")[-1]["body"]["data"]["attributes"]
    assert sent["filter"]["query"] == f"(service:shop-api) AND (trace_id:{TRACE_ID})"
    assert body["trace_id"] == TRACE_ID
    assert [s["span_id"] for s in body["spans"]] == ["10aa", "11bb", "12cc"]
    assert [s["parent_id"] for s in body["spans"]] == ["0", "10aa", "10aa"]
    root = body["spans"][0]
    assert root["duration_ns"] == 9_000_000
    assert root["status"] == "error"
    assert root["resource"] == "POST /checkout"
    assert root["service"] == "shop-api"


def test_a_trace_outside_the_scope_reads_exactly_as_one_that_does_not_exist(datadog):
    # Outside the scope, the scoped span search matches nothing...
    datadog.trace = {"data": []}
    outside = _client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}")
    # ... and an id of no trace at all is the same answer.
    missing = _client(CHILD_A).get("/api/telemetry/traces/ffffffffffff")

    assert outside.status_code == missing.status_code == 404
    assert outside.json() == missing.json()
    detail = outside.json()["detail"]
    assert isinstance(detail, str)
    assert detail.endswith(".")
    assert TRACE_ID not in detail


def test_the_not_found_sentence_says_traces_are_sampled(datadog):
    # Datadog keeps a sample of traces, so a trace id from an ordinary log line
    # often has no trace. The one sentence says so, for every not-found case.
    datadog.trace = {"data": []}

    detail = _client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}").json()["detail"]

    assert detail == (
        "This trace was not found in your organization's traces. Datadog keeps only a "
        "sample of traces, so most ordinary requests have none."
    )


def test_a_malformed_trace_id_is_the_same_404_and_sends_nothing(datadog):
    response = _client(CHILD_A).get("/api/telemetry/traces/not-a-trace")

    assert response.status_code == 404
    datadog.trace = {"data": []}
    assert response.json() == _client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}").json()
    assert len(datadog.sent("/spans/events/search")) == 1


def test_personal_data_in_spans_is_masked(datadog):
    datadog.spans = {
        "data": [_span("5a1", "0", "2026-11-25T10:00:00Z", resource="GET /users/alice@example.com")]
    }
    datadog.trace = {
        "data": [_span("5a1", "0", "2026-11-25T10:00:00Z", tags=["user:bob@example.com"])]
    }

    listing = _ok(_client(CHILD_A).get("/api/telemetry/traces"))
    trace = _ok(_client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}"))

    assert "example.com" not in json.dumps(listing)
    assert "example.com" not in json.dumps(trace)
    assert listing["rows"][0]["resource"] == "GET /users/<email>"
    assert trace["spans"][0]["tags"] == ["user:<email>"]


@pytest.mark.parametrize("path", ["/api/telemetry/traces", f"/api/telemetry/traces/{TRACE_ID}"])
def test_trace_reads_are_audited_once_with_counts(path, audit_rows):
    _ok(_client(CHILD_A).get(path))

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["tenant_id"] == CHILD_A
    assert details["outcome"] == "ok"
    assert "POST /checkout" not in json.dumps(rows[0])
    if path.endswith(TRACE_ID):
        assert rows[0]["event_type"] == "telemetry.trace.read"
        assert details["trace_id"] == TRACE_ID
        assert details["spans"] == 3
    else:
        assert rows[0]["event_type"] == "telemetry.traces.read"
        assert details["rows"] == 1


def test_a_missing_trace_is_audited_as_not_found(datadog, audit_rows):
    datadog.trace = {"data": []}

    _client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}")

    rows = _telemetry_rows(audit_rows, "telemetry.trace.read")
    assert len(rows) == 1
    assert rows[0]["details"]["outcome"] == "not_found"
    assert rows[0]["details"]["spans"] == 0


# ─── monitors ──────────────────────────────────────────────────────────


def test_monitors_are_the_tenants_every_page_of_them(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors"))

    sent = datadog.sent("/api/v1/monitor/search")
    assert [r["params"]["query"] for r in sent] == ['tag:"team:shop"', 'tag:"team:shop"']
    assert [r["params"]["page"] for r in sent] == ["0", "1"]
    assert [m["name"] for m in body["monitors"]] == [
        "Checkout errors",
        "Cart latency",
        "Payments no data",
        "Queue depth",
        "Muted checkout",
    ]
    first = body["monitors"][0]
    assert first["id"] == "1"
    assert first["status"] == "Alert"
    assert first["firing"] is True
    assert first["type"] == "log alert"
    assert first["last_triggered"] == "2025-11-25T10:00:00+00:00"
    assert body["monitors"][1]["firing"] is False
    assert body["monitors"][1]["last_triggered"] is None
    assert body["firing"] is False


def test_firing_keeps_alert_warn_and_no_data(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors", params={"firing": "true"}))

    assert [m["status"] for m in body["monitors"]] == ["Alert", "No Data", "Warn"]
    assert body["firing"] is True


def test_firing_is_asked_of_datadog_without_muted_monitors(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors", params={"firing": "true"}))

    queries = {r["params"]["query"] for r in datadog.sent("/api/v1/monitor/search")}
    assert queries == {f'(tag:"team:shop") AND ({FIRING_FILTER})'}
    assert "Muted checkout" not in [m["name"] for m in body["monitors"]]


def test_firing_ands_the_callers_query_inside_the_filter(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/monitors", params={"firing": "true", "q": "checkout"}))

    assert datadog.sent("/api/v1/monitor/search")[0]["params"]["query"] == (
        f'(tag:"team:shop") AND ({FIRING_FILTER} AND (checkout))'
    )


def test_a_refused_query_with_firing_sends_nothing(datadog):
    response = _client(CHILD_A).get(
        "/api/telemetry/monitors", params={"firing": "true", "q": "a) OR (b"}
    )

    assert response.status_code == 400
    assert datadog.requests == []


def test_every_monitor_includes_the_muted_ones(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors"))

    assert "Muted checkout" in [m["name"] for m in body["monitors"]]


def test_a_monitor_query_is_anded_inside_the_scope(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/monitors", params={"q": "checkout"}))

    assert datadog.sent("/api/v1/monitor/search")[0]["params"]["query"] == (
        '(tag:"team:shop") AND (checkout)'
    )


@pytest.mark.parametrize("path", ["/api/telemetry/traces", f"/api/telemetry/traces/{TRACE_ID}"])
def test_a_tenant_whose_spans_are_not_shared_says_so(path, monkeypatch, datadog, audit_rows):
    # Span access is opt-in: a scope without a ``spans`` filter grants none.
    from robothor.settings import reset_settings

    monkeypatch.setenv(
        "ROBOTHOR_DATADOG_SCOPES", json.dumps({**SCOPES, CHILD_A: {"logs": "service:shop-api"}})
    )
    reset_settings()

    response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    assert response.json() == {
        "status": "no_access",
        "message": "Traces are not shared with this organization.",
        "scope": CHILD_A,
    }
    assert datadog.requests == []
    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["details"]["outcome"] == "no_access"


def test_a_credential_named_span_tag_is_replaced(datadog):
    datadog.trace = {
        "data": [
            _span("5a1", "0", "2026-11-25T10:00:00Z", tags=["password:hunter2pass", "env:prod"])
        ]
    }

    trace = _ok(_client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}"))

    assert trace["spans"][0]["tags"] == ["password:<redacted>", "env:prod"]


def test_a_tenant_whose_monitors_are_not_shared_says_so(monkeypatch, datadog):
    from robothor.settings import reset_settings

    monkeypatch.setenv(
        "ROBOTHOR_DATADOG_SCOPES", json.dumps({**SCOPES, CHILD_A: {"logs": "service:shop-api"}})
    )
    reset_settings()

    response = _client(CHILD_A).get("/api/telemetry/monitors")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_access"
    assert body["message"] == "Monitors are not shared with this organization."
    assert datadog.requests == []


def test_monitor_names_are_masked(datadog):
    datadog.monitor_pages = [
        {
            "monitors": [_monitor(9, "Paging bob@example.com", "Alert")],
            "metadata": {"page": 0, "page_count": 1},
        }
    ]

    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors"))

    assert body["monitors"][0]["name"] == "Paging <email>"


def test_a_monitor_read_is_audited_once(audit_rows):
    _ok(_client(CHILD_A).get("/api/telemetry/monitors", params={"firing": "true"}))

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "telemetry.monitors.read"
    assert rows[0]["details"]["monitors"] == 3
    assert rows[0]["details"]["firing"] is True
    assert "Checkout errors" not in json.dumps(rows[0])


# ─── states, never a 500 ───────────────────────────────────────────────


@pytest.mark.parametrize("path", TENANT_ROUTES[:3])
def test_a_tenant_with_no_scope_is_not_configured(path, monkeypatch, datadog):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps({ROOT: {"logs": "*"}}))
    reset_settings()

    response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["message"] == NOT_CONNECTED
    assert "reason" not in body
    assert datadog.requests == []


@pytest.mark.parametrize("path", TENANT_ROUTES[:3])
def test_missing_keys_are_not_configured(path):
    from robothor.integrations.datadog.client import DatadogError

    with patch("routers.telemetry._client", side_effect=DatadogError("not_configured")):
        body = _client(CHILD_A).get(path).json()

    assert body["status"] == "not_configured"
    assert body["message"] == NOT_CONNECTED


@pytest.mark.parametrize(("status", "kind"), [(429, "rate_limit"), (403, "auth"), (503, "network")])
@pytest.mark.parametrize("path", TENANT_ROUTES[:3])
def test_a_datadog_failure_is_unavailable(path, status, kind, datadog, audit_rows):
    datadog.status = status

    response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "kind": kind, "scope": CHILD_A}
    assert "vendor text" not in response.text
    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["details"]["outcome"] == "unavailable"


# ─── recorded activity ─────────────────────────────────────────────────


def test_signals_read_both_kinds_for_the_tokens_tenant_only(audit_log):
    body = _ok(_client(CHILD_A).get("/api/telemetry/signals"))

    assert sorted(c["event_type"] for c in audit_log.calls) == [
        "datadog.alert_received",
        "datadog.watch_finding",
    ]
    for call in audit_log.calls:
        assert call["tenants"] == [CHILD_A]
        assert call["limit"] == 50
        assert call["since"]
    # Newest first, across both kinds.
    assert [s["type"] for s in body["signals"]] == ["watch", "alert"]
    assert body["scope"] == CHILD_A


def test_signals_take_a_kind(audit_log):
    alerts = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "alerts"}))
    assert [c["event_type"] for c in audit_log.calls] == ["datadog.alert_received"]
    assert [s["type"] for s in alerts["signals"]] == ["alert"]

    audit_log.calls.clear()
    watch = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "watch"}))
    assert [c["event_type"] for c in audit_log.calls] == ["datadog.watch_finding"]
    assert [s["type"] for s in watch["signals"]] == ["watch"]


@pytest.mark.parametrize("kind", ["all", "logs", "alert"])
def test_an_unknown_kind_is_a_422(kind, audit_log):
    response = _client(CHILD_A).get("/api/telemetry/signals", params={"kind": kind})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert audit_log.calls == []


def test_signals_take_since_and_limit(audit_log):
    _ok(
        _client(CHILD_A).get(
            "/api/telemetry/signals", params={"since": "2026-11-20T00:00:00Z", "limit": "5"}
        )
    )

    for call in audit_log.calls:
        assert call["since"] == "2026-11-20T00:00:00+00:00"
        assert call["limit"] == 5


def test_a_relative_since_becomes_a_timestamp(audit_log):
    from datetime import UTC, datetime, timedelta

    _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"since": "24h"}))

    since = datetime.fromisoformat(audit_log.calls[0]["since"])
    expected = datetime.now(UTC) - timedelta(hours=24)
    assert abs((since - expected).total_seconds()) < 60


def test_an_alert_is_what_happened_and_what_would_have_been_sent(audit_log):
    body = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "alerts"}))

    alert = body["signals"][0]
    assert alert["type"] == "alert"
    assert alert["id"] == 11
    assert alert["timestamp"] == "2026-11-25T10:00:05+00:00"
    assert alert["sent"] is False
    assert alert["mode"] == "dry_run"
    assert alert["title"] == "Checkout errors for <email>"
    assert alert["transition"] == "triggered"
    assert alert["priority"] == "P2"
    assert alert["monitor_id"] == "1234"
    assert alert["link"] == "https://app.example.com/monitors/1234"
    assert alert["recipients"] == "the instance operator, by telegram and email"
    assert alert["level"] == "critical"
    assert alert["would_run"] == {
        "agent": "main",
        "prompt": "Datadog monitor 1234 (Checkout errors) changed to triggered. Change nothing.",
    }


def test_a_watch_finding_is_what_happened_and_what_would_have_been_sent(audit_log):
    body = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "watch"}))

    finding = body["signals"][0]
    assert finding["type"] == "watch"
    assert finding["id"] == 12
    assert finding["sent"] is False
    assert finding["finding"] == "error_spike"
    assert finding["service"] == "shop-api"
    assert finding["count"] == 40
    assert finding["window"] == {
        "start": "2026-11-25T09:45:00+00:00",
        "end": "2026-11-25T10:00:00+00:00",
        "minutes": 15,
    }
    assert finding["message"] == (
        "shop-api logged 40 errors in the 15 minutes to 10:00 UTC. Ask <email>."
    )
    assert finding["recipients"] == "main agent alert digest, tenant acme-child-a"


def _one(audit_log, kind: str, event: dict[str, Any]) -> dict[str, Any]:
    event_type = "datadog.alert_received" if kind == "alerts" else "datadog.watch_finding"
    audit_log.by_type[event_type] = [event]
    body = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": kind}))
    return body["signals"][0]


def test_a_delivered_finding_says_it_was_sent(audit_log):
    finding = _one(
        audit_log,
        "watch",
        _watch_event(mode="delivered", delivery={"route": "alert_digest", "text": "x"}),
    )

    assert finding["sent"] is True
    assert finding["mode"] == "delivered"
    assert finding["reason"] is None
    assert finding["delivery_route"] == "alert_digest"


@pytest.mark.parametrize("mode", ["deliver", "dry_run", "something_new", None, 7])
def test_only_delivered_counts_as_sent(mode, audit_log):
    finding = _one(audit_log, "watch", _watch_event(mode=mode))

    assert finding["sent"] is False


def test_an_older_record_without_a_mode_reads_as_dry_run(audit_log):
    event = _watch_event()
    del event["details"]["mode"]

    finding = _one(audit_log, "watch", event)

    assert finding["mode"] == "dry_run"
    assert finding["sent"] is False


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("suppressed", "episode_open"),
        ("suppressed", "cooldown"),
        ("suppressed", "covered_by_webhook"),
        ("delivery_failed", "no_engine"),
        ("delivery_failed", "handoff_failed"),
        ("delivery_failed", "rate_limit"),
    ],
)
def test_a_record_that_was_not_sent_carries_its_mode_and_reason(mode, reason, audit_log):
    finding = _one(audit_log, "watch", _watch_event(mode=mode, reason=reason))
    alert = _one(audit_log, "alerts", _alert_event(mode=mode, reason=reason))

    for signal in (finding, alert):
        assert signal["sent"] is False
        assert signal["mode"] == mode
        assert signal["reason"] == reason


@pytest.mark.parametrize(
    ("mode", "delivery", "reason"),
    [
        ("suppressed", {"level": "warning", "reason": "episode_open"}, "episode_open"),
        ("delivery_failed", {"reason": "no_engine", "tenant": CHILD_A}, "no_engine"),
        ("delivery_failed", {"level": "warning", "error": "not_delivered"}, "not_delivered"),
    ],
)
def test_the_reason_the_engine_writes_is_the_reason_shown(mode, delivery, reason, audit_log):
    """A record as the engine's alert intake writes it, read back by this router."""
    from robothor.integrations.datadog import alerts as dd

    written: list[dict[str, Any]] = []

    def log_event(*args: Any, **kw: Any) -> dict[str, Any]:
        written.append(kw["details"])
        return {"id": 1}

    alert = dd.parse_alert(
        {"monitor_id": "1234", "transition": "Triggered", "tags": f"org:{CHILD_A}"}
    )
    with patch("robothor.audit.logger.log_event", side_effect=log_event):
        dd._record(alert, CHILD_A, "org", "Checkout errors", {"mode": mode, "delivery": delivery})

    (details,) = written
    signal = _one(audit_log, "alerts", _alert_event(**details))
    assert signal["mode"] == mode
    assert signal["reason"] == reason


def test_a_delivered_alert_reads_its_delivery_and_triage(audit_log):
    alert = _one(
        audit_log,
        "alerts",
        _alert_event(
            mode="delivered",
            delivery={"route": "page", "channels": ["telegram"], "text": "Checkout errors"},
            triage={"started": True, "run_id": "r-1", "agent": "main"},
        ),
    )

    assert alert["sent"] is True
    assert alert["delivery_route"] == "page"
    assert alert["triage"] == {"started": True, "reason": None}
    # The fallback fields stay, for the page to show when delivery says less.
    assert alert["recipients"] == "the instance operator, by telegram and email"
    assert alert["would_run"]["agent"] == "main"


def test_a_triage_that_was_not_started_says_why(audit_log):
    alert = _one(
        audit_log,
        "alerts",
        _alert_event(mode="delivered", triage={"started": False, "reason": "no_engine"}),
    )

    assert alert["triage"] == {"started": False, "reason": "no_engine"}


@pytest.mark.parametrize(
    "delivery",
    ["alert_digest", ["page"], 3, {"channel": "telegram"}, {"route": {"odd": 1}}, None],
)
@pytest.mark.parametrize("triage", ["started", ["x"], 0, {"run_id": "r-2"}, {}, None])
def test_any_shape_of_delivery_and_triage_reads(delivery, triage, audit_log):
    alert = _one(
        audit_log,
        "alerts",
        _alert_event(mode="delivered", delivery=delivery, triage=triage, surprise={"k": 1}),
    )

    assert alert["delivery_route"] is None or isinstance(alert["delivery_route"], str)
    assert alert["triage"] is None or isinstance(alert["triage"]["started"], bool)
    assert "surprise" not in alert


def test_delivery_and_triage_text_is_masked(audit_log):
    alert = _one(
        audit_log,
        "alerts",
        _alert_event(
            mode="delivery_failed",
            reason="mail to bob@example.com bounced",
            delivery={"route": "page to alice@example.com"},
            triage={"started": False, "reason": "asked alice@example.com"},
        ),
    )

    assert "example.com/monitors" in alert["link"]
    text = json.dumps({k: v for k, v in alert.items() if k != "link"})
    assert "@example.com" not in text


def test_signals_carry_only_what_the_page_shows(audit_log):
    body = _ok(_client(CHILD_A).get("/api/telemetry/signals"))

    text = json.dumps(body)
    assert "example.com/monitors" in text  # the monitor's own link survives
    assert "bob@example.com" not in text
    assert "alice@example.com" not in text
    for key in ("event_id", "routing", "datadog_transition", "baseline", "tags", "actor"):
        assert all(key not in s for s in body["signals"]), key


def test_a_link_that_is_not_https_is_dropped(audit_log):
    audit_log.by_type["datadog.alert_received"] = [_alert_event(link="javascript:alert(1)")]

    body = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "alerts"}))

    assert body["signals"][0]["link"] is None


def test_a_malformed_recorded_row_does_not_break_the_list(audit_log):
    broken = _alert_event()
    broken["details"] = "not a dict"
    audit_log.by_type["datadog.alert_received"] = [broken, _alert_event()]

    body = _ok(_client(CHILD_A).get("/api/telemetry/signals", params={"kind": "alerts"}))

    assert len(body["signals"]) == 2
    assert body["signals"][0]["sent"] is False


def test_an_acting_root_admin_reads_the_childs_signals(audit_log):
    _ok(_client(CHILD_A, "admin", home=ROOT).get("/api/telemetry/signals"))

    assert {tuple(c["tenants"]) for c in audit_log.calls} == {(CHILD_A,)}


def test_a_signals_read_is_audited_once_with_counts(audit_rows):
    _ok(_client(CHILD_A).get("/api/telemetry/signals"))

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "telemetry.signals.read"
    assert rows[0]["details"]["signals"] == 2
    assert rows[0]["details"]["kind"] == "all"
    assert "shop-api logged" not in json.dumps(rows[0])


# ─── the root's overview ───────────────────────────────────────────────


def _by_tenant(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["tenant_id"]: t for t in body["tenants"]}


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_the_root_at_home_sees_every_accessible_tenant(role, datadog):
    body = _ok(_client(ROOT, role).get("/api/telemetry/overview"))

    assert [t["tenant_id"] for t in body["tenants"]] == [ROOT, CHILD_A, CHILD_B]
    tenants = _by_tenant(body)
    assert tenants[CHILD_A]["connected"] is True
    assert tenants[CHILD_A]["errors"] == 5
    # Monitor pages 0 and 1: Alert, No Data and Warn are firing.
    assert tenants[CHILD_A]["firing_monitors"] == 3
    assert tenants[CHILD_A]["error_kind"] is None
    assert tenants[ROOT]["errors"] == 12
    assert body["since"] == "1h"


def test_each_tenant_is_read_inside_its_own_scope(datadog):
    _ok(_client(ROOT).get("/api/telemetry/overview"))

    aggregates = sorted(
        r["body"]["filter"]["query"] for r in datadog.sent("/logs/analytics/aggregate")
    )
    assert aggregates == [
        "(service:clinic-app) AND (status:error)",
        "(service:shop-api) AND (status:error)",
        "status:error",
    ]
    monitor_queries = {r["params"].get("query", "") for r in datadog.sent("/api/v1/monitor/search")}
    assert monitor_queries == {
        FIRING_FILTER,
        f'(tag:"team:shop") AND ({FIRING_FILTER})',
        f'(tag:"team:clinic") AND ({FIRING_FILTER})',
    }


def test_the_tenant_list_never_comes_from_the_request(datadog):
    body = _ok(
        _client(ROOT).get(
            "/api/telemetry/overview", params={"tenants": "acme-other", "tenant_id": "acme-other"}
        )
    )

    assert [t["tenant_id"] for t in body["tenants"]] == [ROOT, CHILD_A, CHILD_B]
    assert "acme-other" not in json.dumps(datadog.requests)


def test_a_tenant_with_no_scope_is_not_connected(monkeypatch, datadog):
    from robothor.settings import reset_settings

    monkeypatch.setenv(
        "ROBOTHOR_DATADOG_SCOPES", json.dumps({k: v for k, v in SCOPES.items() if k != CHILD_B})
    )
    reset_settings()

    tenants = _by_tenant(_ok(_client(ROOT).get("/api/telemetry/overview")))

    assert tenants[CHILD_B] == {
        "tenant_id": CHILD_B,
        "connected": False,
        "errors": None,
        "firing_monitors": None,
        "error_kind": None,
    }
    assert tenants[CHILD_A]["connected"] is True


def test_a_tenant_whose_monitors_are_not_shared_has_no_monitor_count(monkeypatch, datadog):
    from robothor.settings import reset_settings

    scopes = {**SCOPES, CHILD_B: {"logs": "service:clinic-app"}}
    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps(scopes))
    reset_settings()

    tenants = _by_tenant(_ok(_client(ROOT).get("/api/telemetry/overview")))

    assert tenants[CHILD_B]["connected"] is True
    assert tenants[CHILD_B]["firing_monitors"] is None
    assert tenants[CHILD_B]["error_kind"] is None
    assert isinstance(tenants[CHILD_B]["errors"], int)


def test_one_tenant_failing_never_fails_the_response(datadog):
    datadog.failing = {"service:shop-api": 429, 'tag:"team:shop"': 429}

    body = _ok(_client(ROOT).get("/api/telemetry/overview"))
    tenants = _by_tenant(body)

    assert tenants[CHILD_A]["connected"] is True
    assert tenants[CHILD_A]["errors"] is None
    assert tenants[CHILD_A]["firing_monitors"] is None
    assert tenants[CHILD_A]["error_kind"] == "rate_limit"
    assert tenants[CHILD_B]["errors"] is not None
    assert tenants[CHILD_B]["error_kind"] is None
    assert "vendor text" not in json.dumps(body)


def test_an_unexpected_failure_for_one_tenant_is_reported_as_unknown(datadog):
    real = datadog.client

    def flaky():
        client = real()
        original = client.aggregate_logs

        def aggregate(scope, *a, **kw):
            if scope.tenant_id == CHILD_B:
                raise RuntimeError("boom")
            return original(scope, *a, **kw)

        client.aggregate_logs = aggregate
        return client

    with patch("routers.telemetry._client", side_effect=flaky):
        tenants = _by_tenant(_ok(_client(ROOT).get("/api/telemetry/overview")))

    assert tenants[CHILD_B]["error_kind"] == "unknown"
    assert tenants[CHILD_A]["error_kind"] is None


def test_the_overview_with_no_keys_is_not_configured_with_the_reason(datadog):
    from robothor.integrations.datadog.client import DatadogError

    with patch("routers.telemetry._client", side_effect=DatadogError("not_configured")):
        body = _client(ROOT).get("/api/telemetry/overview").json()

    assert body["status"] == "not_configured"
    assert body["reason"] == "keys"


def test_the_overview_takes_a_window(datadog):
    body = _ok(_client(ROOT).get("/api/telemetry/overview", params={"since": "24h"}))

    for request in datadog.sent("/logs/analytics/aggregate"):
        assert request["body"]["filter"]["from"] == "now-24h"
    assert body["since"] == "24h"


@pytest.mark.parametrize("role", ["auditor", "member", "viewer"])
def test_the_overview_is_for_owners_and_admins(role, datadog):
    assert _client(ROOT, role).get("/api/telemetry/overview").status_code == 403
    assert datadog.requests == []


def test_the_overview_is_refused_while_acting_in_a_child(datadog, audit_rows):
    response = _client(CHILD_A, "owner", home=ROOT).get("/api/telemetry/overview")

    assert response.status_code == 403
    assert datadog.requests == []
    assert _telemetry_rows(audit_rows) == []


def test_the_overview_is_refused_to_a_childs_own_admin(datadog):
    assert _client(CHILD_A, "owner").get("/api/telemetry/overview").status_code == 403
    assert datadog.requests == []


def test_the_overview_is_refused_to_a_service_token(datadog):
    assert _service_client(ROOT).get("/api/telemetry/overview").status_code == 403
    assert datadog.requests == []


def test_each_tenant_read_is_audited_under_that_tenant(audit_rows):
    _ok(_client(ROOT).get("/api/telemetry/overview"))

    rows = _telemetry_rows(audit_rows, "telemetry.overview")
    assert sorted(r["tenant_id"] for r in rows) == sorted([ROOT, CHILD_A, CHILD_B])
    for row in rows:
        assert row["actor"] == "alice"
        assert row["details"]["home_tenant_id"] == ROOT
        assert row["details"]["target_tenant_id"] == row["tenant_id"]
        assert row["details"]["outcome"] == "ok"
        assert "errors" in row["details"]
    assert len(_telemetry_rows(audit_rows)) == 3


# ─── credentials, as well as personal data ─────────────────────────────

#: A credential shape the platform redactor knows, built from parts so no
#: scanner reads this file as carrying a real secret.
BEARER = "sk-" + "abc123def456ghi789jkl012mno345"
LEAK = f"Authorization: Bearer {BEARER}"


def test_a_credential_in_a_span_is_redacted(datadog):
    datadog.spans = {"data": [_span("5a1", "0", "2026-11-25T10:00:00Z", resource=f"GET /x {LEAK}")]}
    datadog.trace = {
        "data": [_span("5a1", "0", "2026-11-25T10:00:00Z", resource=LEAK, tags=[f"auth:{LEAK}"])]
    }

    listing = _ok(_client(CHILD_A).get("/api/telemetry/traces"))
    trace = _ok(_client(CHILD_A).get(f"/api/telemetry/traces/{TRACE_ID}"))

    for body in (listing, trace):
        assert BEARER not in json.dumps(body)
        assert "<redacted>" in json.dumps(body)


def test_a_credential_in_a_monitor_name_is_redacted(datadog):
    datadog.monitor_pages = [
        {"monitors": [_monitor(9, f"Probe {LEAK}", "Alert")], "metadata": {"page_count": 1}}
    ]

    body = _ok(_client(CHILD_A).get("/api/telemetry/monitors"))

    assert BEARER not in json.dumps(body)
    assert "<redacted>" in body["monitors"][0]["name"]


def test_a_credential_in_a_recorded_signal_is_redacted(audit_log):
    audit_log.by_type["datadog.alert_received"] = [
        _alert_event(
            title=f"Checkout {LEAK}",
            reason=LEAK,
            would_run={"agent": "main", "prompt": f"Look at {LEAK}"},
            delivery={"route": LEAK},
            triage={"started": False, "reason": LEAK},
        )
    ]
    audit_log.by_type["datadog.watch_finding"] = [_watch_event(brief=f"Spike, see {LEAK}")]

    body = _ok(_client(CHILD_A).get("/api/telemetry/signals"))

    assert BEARER not in json.dumps(body)
    assert "<redacted>" in json.dumps(body)


@pytest.mark.parametrize(
    ("path", "event"),
    [
        ("/api/telemetry/traces", "telemetry.traces.read"),
        ("/api/telemetry/monitors", "telemetry.monitors.read"),
    ],
)
def test_a_credential_in_the_query_is_redacted_in_the_audit_row(path, event, audit_rows):
    _client(CHILD_A).get(path, params={"q": f'"{BEARER}"'})

    rows = _telemetry_rows(audit_rows, event)
    assert len(rows) == 1
    assert BEARER not in json.dumps(rows[0])
