"""The organization's Logs page: its tenant's lines from the shared Datadog account.

What this file pins:

* the tenant comes from the TOKEN, never from the request: a query parameter
  naming another tenant changes nothing, and a root admin acting in a child
  reads the child's scope;
* tenant owners, admins and auditors read; a viewer or member is refused;
* the route never 500s: no keys or no scope is ``not_configured`` (one plain
  sentence for anyone but the root at home), a Datadog failure is
  ``unavailable`` with its kind, and a query the scope wall refuses is a 400
  carrying the plain reason before anything is sent;
* one audit row per read, naming the redacted query, the window and the row
  count, never a line's text;
* email addresses and phone numbers in a row are masked before they leave.

Datadog is reached through the router's ``_client`` seam, answered by an
``httpx.MockTransport`` so the composed request itself is asserted.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

SCOPES = {
    ROOT: {"logs": "*"},
    CHILD_A: {"logs": "service:shop-api"},
    CHILD_B: {"logs": "service:clinic-app"},
}

NOT_CONNECTED = "Datadog is not connected for this organization yet."


def _search_body(rows: list[dict[str, Any]] | None = None, after: str | None = None) -> dict:
    return {
        "data": rows
        if rows is not None
        else [
            {
                "id": "log-1",
                "attributes": {
                    "timestamp": "2026-11-25T10:00:00Z",
                    "service": "shop-api",
                    "status": "error",
                    "host": "web-1",
                    "message": "checkout failed",
                    "tags": ["env:prod", "service:shop-api"],
                    "attributes": {"http": {"status_code": 500}},
                },
            }
        ],
        "meta": {"page": {"after": after}} if after else {},
    }


AGGREGATE_BODY = {
    "data": {
        "buckets": [
            {"by": {"service": "shop-api"}, "computes": {"c0": 7}},
            {"by": {"service": "clinic-app"}, "computes": {"c0": 2}},
        ]
    }
}


class FakeDatadog:
    """Records every request the real client sends, and answers it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.search = _search_body()
        self.aggregate: dict[str, Any] = AGGREGATE_BODY
        self.raise_network = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectError("refused")
        body = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["vendor text"]})
        if request.url.path.endswith("/aggregate"):
            return httpx.Response(200, json=self.aggregate)
        return httpx.Response(200, json=self.search)

    def client(self):
        from robothor.integrations.datadog.client import DatadogClient

        return DatadogClient("api-key", "app-key", transport=httpx.MockTransport(self.handler))

    @property
    def last(self) -> dict[str, Any]:
        assert self.requests, "no request reached Datadog"
        return self.requests[-1]["body"]


@pytest.fixture
def datadog():
    return FakeDatadog()


@pytest.fixture
def audit_rows():
    return []


@pytest.fixture(autouse=True)
def _env(monkeypatch, datadog, audit_rows):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    # Acting tokens are honoured only while switching is enforced.
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


def _ok(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok", body
    return body


# ─── the tenant wall ───────────────────────────────────────────────────


def test_a_tenant_admin_reads_its_own_scope(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/logs", params={"q": "status:error"}))

    assert datadog.last["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert body["scope"] == CHILD_A
    assert body["rows"][0]["service"] == "shop-api"
    assert body["rows"][0]["message"] == "checkout failed"
    assert body["rows"][0]["tags"] == ["env:prod", "service:shop-api"]


def test_an_empty_query_reads_the_whole_scope(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/logs"))

    assert datadog.last["filter"]["query"] == "service:shop-api"


def test_a_tenant_named_in_the_request_changes_nothing(datadog):
    body = _ok(
        _client(CHILD_A).get(
            "/api/telemetry/logs", params={"tenant_id": CHILD_B, "tenant": CHILD_B}
        )
    )

    assert datadog.last["filter"]["query"] == "service:shop-api"
    assert body["scope"] == CHILD_A


def test_a_root_admin_acting_in_a_child_reads_the_childs_scope(datadog):
    body = _ok(_client(CHILD_A, "admin", home=ROOT).get("/api/telemetry/logs"))

    assert datadog.last["filter"]["query"] == "service:shop-api"
    assert body["scope"] == CHILD_A


def test_the_root_admin_at_home_reads_the_root_scope(datadog):
    body = _ok(_client(ROOT).get("/api/telemetry/logs", params={"q": "service:shop-api"}))

    assert datadog.last["filter"]["query"] == "service:shop-api"
    assert body["scope"] == ROOT


# ─── who may read ──────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["owner", "admin", "auditor"])
def test_owners_admins_and_auditors_read(role):
    _ok(_client(CHILD_A, role).get("/api/telemetry/logs"))
    _ok(_client(CHILD_A, role).get("/api/telemetry/logs/summary"))


@pytest.mark.parametrize("role", ["viewer", "member", "user"])
@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_any_other_role_is_refused(role, path, datadog, audit_rows):
    response = _client(CHILD_A, role).get(path)

    assert response.status_code == 403
    assert datadog.requests == []
    assert [r for r in audit_rows if r["event_type"].startswith("telemetry.")] == []


def test_the_shared_viewer_fixture_is_refused(controls_client_as_viewer, datadog):
    response = controls_client_as_viewer.get("/api/telemetry/logs")

    assert response.status_code == 403
    assert datadog.requests == []


def test_the_shared_operator_fixture_reads(controls_client_as_operator, monkeypatch, datadog):
    # The fixture's tenant is the bridge's platform tenant; give it a scope.
    from routers.controls import PLATFORM_TENANT

    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps({PLATFORM_TENANT: {"logs": "*"}}))
    reset_settings()

    body = _ok(controls_client_as_operator.get("/api/telemetry/logs", params={"q": "env:prod"}))

    assert datadog.last["filter"]["query"] == "env:prod"
    assert body["scope"] == PLATFORM_TENANT


# ─── states, never a 500 ───────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_a_tenant_with_no_scope_is_not_configured(path, monkeypatch, datadog):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps({ROOT: {"logs": "*"}}))
    reset_settings()

    response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["message"] == NOT_CONNECTED
    # Which half is missing is the root's business, not the child's.
    assert "reason" not in body
    assert datadog.requests == []


@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_missing_keys_are_not_configured(path, datadog):
    from robothor.integrations.datadog.client import DatadogError

    with patch("routers.telemetry._client", side_effect=DatadogError("not_configured")):
        response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["message"] == NOT_CONNECTED
    assert "reason" not in body


def test_the_root_at_home_is_told_which_half_is_missing(monkeypatch):
    from robothor.integrations.datadog.client import DatadogError
    from robothor.settings import reset_settings

    with patch("routers.telemetry._client", side_effect=DatadogError("not_configured")):
        keys = _client(ROOT).get("/api/telemetry/logs").json()
    assert keys["status"] == "not_configured"
    assert keys["reason"] == "keys"

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps({CHILD_A: {"logs": "*"}}))
    reset_settings()
    scope = _client(ROOT).get("/api/telemetry/logs").json()
    assert scope["status"] == "not_configured"
    assert scope["reason"] == "scope"


@pytest.mark.parametrize(("status", "kind"), [(429, "rate_limit"), (403, "auth"), (503, "network")])
@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_a_datadog_failure_is_unavailable(path, status, kind, datadog):
    datadog.status = status

    response = _client(CHILD_A).get(path)

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "unavailable", "kind": kind, "scope": CHILD_A}
    assert "vendor text" not in response.text


def test_an_unreachable_datadog_is_unavailable(datadog):
    datadog.raise_network = True

    body = _client(CHILD_A).get("/api/telemetry/logs").json()

    assert body["status"] == "unavailable"
    assert body["kind"] == "network"


@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_a_refused_query_is_a_400_with_the_reason(path, datadog):
    response = _client(CHILD_A).get(path, params={"q": "a) OR (b"})

    assert response.status_code == 400
    assert response.json() == {"detail": "the brackets or parentheses close out of order"}
    assert datadog.requests == []


# ─── parameters ────────────────────────────────────────────────────────


def test_the_window_defaults_to_the_last_hour(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/logs"))

    assert datadog.last["filter"]["from"] == "now-1h"
    assert datadog.last["filter"]["to"] == "now"


@pytest.mark.parametrize(
    ("since", "sent"),
    [
        ("15m", "now-15m"),
        ("24h", "now-24h"),
        ("7d", "now-7d"),
        ("30s", "now-30s"),
        ("2026-11-24T08:00:00Z", "2026-11-24T08:00:00+00:00"),
    ],
)
def test_since_takes_the_observe_pages_values(since, sent, datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/logs", params={"since": since}))

    assert datadog.last["filter"]["from"] == sent


def test_until_takes_the_same_values(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/logs", params={"since": "2d", "until": "2026-11-25"}))

    assert datadog.last["filter"]["from"] == "now-2d"
    assert datadog.last["filter"]["to"] == "2026-11-25T00:00:00"


@pytest.mark.parametrize("field", ["since", "until"])
@pytest.mark.parametrize("value", ["yesterday", "2 hours ago", "1h\n", "2026-13-01", "-1h"])
def test_a_time_the_route_cannot_read_is_a_flat_422(field, value, datadog):
    response = _client(CHILD_A).get("/api/telemetry/logs", params={field: value})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith(field)
    assert datadog.requests == []


def test_limit_and_cursor_page_on(datadog):
    datadog.search = _search_body(after="cursor-2")

    body = _ok(
        _client(CHILD_A).get("/api/telemetry/logs", params={"limit": "20", "cursor": "cursor-1"})
    )

    assert datadog.last["page"] == {"limit": 20, "cursor": "cursor-1"}
    assert body["next_cursor"] == "cursor-2"


def test_the_last_page_has_no_cursor(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/logs"))

    assert body["next_cursor"] is None


@pytest.mark.parametrize("limit", ["0", "-5", "ten", "1_0"])
def test_a_limit_that_is_not_a_positive_number_is_a_422(limit, datadog):
    response = _client(CHILD_A).get("/api/telemetry/logs", params={"limit": limit})

    assert response.status_code == 422
    assert datadog.requests == []


# ─── masking ───────────────────────────────────────────────────────────


def test_personal_data_in_a_row_is_masked(datadog):
    datadog.search = _search_body(
        rows=[
            {
                "id": "log-2",
                "attributes": {
                    "timestamp": "2026-11-25T10:00:00Z",
                    "service": "shop-api",
                    "status": "error",
                    "host": "web-1",
                    "message": "mail to alice@example.com bounced, call +1 415 555 0100",
                    "tags": ["user:bob@example.com"],
                    "attributes": {
                        "usr": {"email": "bob@example.com", "id": 42},
                        "contacts": ["agent@example.com", 7],
                    },
                },
            }
        ]
    )

    body = _ok(_client(CHILD_A).get("/api/telemetry/logs"))
    row = body["rows"][0]

    assert "example.com" not in json.dumps(body)
    assert "415 555 0100" not in row["message"]
    assert "<email>" in row["message"]
    assert "<phone>" in row["message"]
    assert row["tags"] == ["user:<email>"]
    assert row["attributes"]["usr"] == {"email": "<email>", "id": 42}
    assert row["attributes"]["contacts"] == ["<email>", 7]


#: Credential shapes the platform redactor knows. Built from parts so no
#: scanner reads this file as carrying a real secret.
BEARER = "sk-" + "abc123def456ghi789jkl012mno345"
DSN_PASSWORD = "hunter2" + "pass"


def test_credentials_in_a_row_are_redacted(datadog):
    datadog.search = _search_body(
        rows=[
            {
                "id": "log-3",
                "attributes": {
                    "timestamp": "2026-11-25T10:00:00Z",
                    "service": "shop-api",
                    "status": "error",
                    "host": "web-1",
                    "message": f"upstream refused Authorization: Bearer {BEARER}",
                    "tags": [f"dsn:postgres://bob:{DSN_PASSWORD}@db:5432/shop"],
                    "attributes": {
                        "http": {"url": f"https://api.example.invalid/v1?api_key={BEARER}"},
                        "headers": [f"Authorization: Bearer {BEARER}"],
                    },
                },
            }
        ]
    )

    body = _ok(_client(CHILD_A).get("/api/telemetry/logs"))
    text = json.dumps(body)

    assert BEARER not in text
    assert DSN_PASSWORD not in text
    row = body["rows"][0]
    assert "<redacted>" in row["message"]
    assert "<redacted>" in row["tags"][0]
    assert "<redacted>" in row["attributes"]["http"]["url"]
    assert "<redacted>" in row["attributes"]["headers"][0]


def test_a_credential_in_a_summary_bucket_is_redacted(datadog):
    datadog.aggregate = {
        "data": {"buckets": [{"by": {"@http.auth": f"Bearer {BEARER}"}, "computes": {"c0": 1}}]}
    }

    body = _ok(
        _client(CHILD_A).get("/api/telemetry/logs/summary", params={"group_by": "@http.auth"})
    )

    assert BEARER not in json.dumps(body)
    assert "<redacted>" in body["buckets"][0]["by"]


@pytest.mark.parametrize("path", ["/api/telemetry/logs", "/api/telemetry/logs/summary"])
def test_a_credential_in_the_query_never_reaches_the_audit_row(path, audit_rows):
    _ok(_client(CHILD_A).get(path, params={"q": f'"{BEARER}"'}))

    row = _telemetry_rows(audit_rows)[0]
    assert BEARER not in json.dumps(row)
    assert "<redacted>" in row["details"]["query"]


# ─── trace correlation ─────────────────────────────────────────────────


def test_a_row_carries_its_trace_and_span_ids(datadog):
    datadog.search = _search_body(
        rows=[
            {
                "id": "log-4",
                "attributes": {
                    "timestamp": "2026-11-25T10:00:00Z",
                    "service": "shop-api",
                    "status": "error",
                    "message": "checkout failed",
                    "trace_id": "8432918201992837465",
                    "span_id": "1029384756",
                    "attributes": {},
                },
            }
        ]
    )

    row = _ok(_client(CHILD_A).get("/api/telemetry/logs"))["rows"][0]

    assert row["trace_id"] == "8432918201992837465"
    assert row["span_id"] == "1029384756"


def test_a_row_without_a_trace_says_so(datadog):
    row = _ok(_client(CHILD_A).get("/api/telemetry/logs"))["rows"][0]

    assert row["trace_id"] is None
    assert row["span_id"] is None


# ─── audit ─────────────────────────────────────────────────────────────


def _telemetry_rows(audit_rows):
    return [r for r in audit_rows if r["event_type"].startswith("telemetry.")]


def test_a_read_writes_one_audit_row_without_the_lines(audit_rows):
    _ok(
        _client(CHILD_A).get(
            "/api/telemetry/logs",
            params={"q": "status:error @usr.email:alice@example.com", "since": "24h"},
        )
    )

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "telemetry.logs.read"
    assert row["actor"]
    details = row["details"]
    assert details["tenant_id"] == CHILD_A
    assert details["query"] == "status:error @usr.email:<email>"
    assert details["since"] == "24h"
    assert details["until"] == "now"
    assert details["rows"] == 1
    assert details["outcome"] == "ok"
    text = json.dumps(row)
    assert "checkout failed" not in text
    assert "alice@example.com" not in text


def test_the_audited_query_is_truncated(audit_rows):
    long_query = "service:shop-api " + "x" * 1000
    _ok(_client(CHILD_A).get("/api/telemetry/logs", params={"q": long_query}))

    details = _telemetry_rows(audit_rows)[0]["details"]
    assert len(details["query"]) < len(long_query)
    assert details["query"].startswith("service:shop-api")


def test_an_acting_read_is_audited_under_the_child(audit_rows):
    _ok(_client(CHILD_A, "admin", home=ROOT).get("/api/telemetry/logs"))

    row = _telemetry_rows(audit_rows)[0]
    assert row["details"]["tenant_id"] == CHILD_A


@pytest.mark.parametrize(
    ("params", "outcome"),
    [({"q": "a) OR (b"}, "refused"), ({}, "unavailable")],
)
def test_a_failed_read_is_audited_too(params, outcome, audit_rows, datadog):
    if outcome == "unavailable":
        datadog.status = 503

    _client(CHILD_A).get("/api/telemetry/logs", params=params)

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["details"]["outcome"] == outcome
    assert rows[0]["details"]["rows"] == 0


def test_a_summary_writes_one_audit_row(audit_rows):
    _ok(_client(CHILD_A).get("/api/telemetry/logs/summary", params={"q": "env:prod"}))

    rows = _telemetry_rows(audit_rows)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "telemetry.logs.summary"
    assert rows[0]["details"]["query"] == "env:prod"
    assert rows[0]["details"]["group_by"] == "service"
    assert rows[0]["details"]["buckets"] == 2


# ─── the summary ───────────────────────────────────────────────────────


def test_the_summary_counts_errors_by_service_by_default(datadog):
    body = _ok(_client(CHILD_A).get("/api/telemetry/logs/summary"))

    sent = datadog.last
    assert sent["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert [g["facet"] for g in sent["group_by"]] == ["service"]
    assert body["buckets"] == [
        {"by": "shop-api", "count": 7},
        {"by": "clinic-app", "count": 2},
    ]
    assert body["group_by"] == "service"
    assert body["errors"] is True
    assert body["scope"] == CHILD_A


def test_the_summary_ands_the_callers_query_inside_the_error_filter(datadog):
    _ok(_client(CHILD_A).get("/api/telemetry/logs/summary", params={"q": "env:prod"}))

    assert datadog.last["filter"]["query"] == (
        "(service:shop-api) AND (status:error AND (env:prod))"
    )


def test_the_summary_counts_everything_when_asked(datadog):
    body = _ok(
        _client(CHILD_A).get(
            "/api/telemetry/logs/summary", params={"errors": "false", "q": "env:prod"}
        )
    )

    assert datadog.last["filter"]["query"] == "(service:shop-api) AND (env:prod)"
    assert body["errors"] is False


def test_the_summary_groups_by_another_facet(datadog):
    datadog.aggregate = {"data": {"buckets": [{"by": {"host": "web-1"}, "computes": {"c0": 3}}]}}

    body = _ok(_client(CHILD_A).get("/api/telemetry/logs/summary", params={"group_by": "host"}))

    assert [g["facet"] for g in datadog.last["group_by"]] == ["host"]
    assert body["buckets"] == [{"by": "web-1", "count": 3}]


def test_a_summary_bucket_value_is_masked(datadog):
    datadog.aggregate = {
        "data": {"buckets": [{"by": {"@usr.email": "bob@example.com"}, "computes": {"c0": 1}}]}
    }

    body = _ok(
        _client(CHILD_A).get("/api/telemetry/logs/summary", params={"group_by": "@usr.email"})
    )

    assert body["buckets"] == [{"by": "<email>", "count": 1}]


@pytest.mark.parametrize("facet", ["", "service host", "service,host", "a)b"])
def test_a_facet_the_route_cannot_read_is_a_422(facet, datadog):
    response = _client(CHILD_A).get("/api/telemetry/logs/summary", params={"group_by": facet})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert datadog.requests == []


# ─── shape of the router ───────────────────────────────────────────────


def test_the_handlers_are_plain_functions():
    from routers import telemetry

    for route in telemetry.router.routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_the_router_is_read_only():
    from routers import telemetry

    methods = {m for route in telemetry.router.routes for m in route.methods}
    assert methods <= {"GET", "HEAD"}
