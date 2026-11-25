"""The Datadog clients: always the composed query, error classes, and keys that never leak."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from robothor.integrations.datadog.client import (
    AsyncDatadogClient,
    Bucket,
    DatadogClient,
    DatadogError,
    LogPage,
    async_client_from_settings,
    client_from_settings,
)
from robothor.integrations.datadog.scope import DatadogScope, ScopeError

API_KEY = "dd-api-key-0123456789abcdef"
APP_KEY = "dd-app-key-fedcba9876543210"
SCOPE = DatadogScope(
    tenant_id="acme-child-a",
    logs="service:shop-api",
    spans="service:shop-api",
    monitors="team:shop",
    metric_tags=("service:shop-api",),
)
LOG_BODY = {
    "data": [{"id": "1", "attributes": {"service": "shop-api", "message": "boom"}}],
    "meta": {"page": {"after": "c2"}},
}
AGG_BODY = {"data": {"buckets": [{"by": {"status": "error"}, "computes": {"c0": 4}}]}}


class Recorder:
    """A MockTransport handler that records each request and answers from a script."""

    def __init__(self, status: int = 200, body: object = None, exc: Exception | None = None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, json=self.body if self.body is not None else {})

    def sent(self, n: int = 0) -> dict:
        return json.loads(self.requests[n].content)


def _sync(rec: Recorder, site: str = "datadoghq.com") -> DatadogClient:
    return DatadogClient(API_KEY, APP_KEY, site=site, transport=httpx.MockTransport(rec))


def _async(rec: Recorder, site: str = "datadoghq.com") -> AsyncDatadogClient:
    return AsyncDatadogClient(API_KEY, APP_KEY, site=site, transport=httpx.MockTransport(rec))


# ── the composed query, never the raw one ───────────────────────────────────


def test_search_logs_sends_the_composed_query() -> None:
    rec = Recorder(body=LOG_BODY)
    page = _sync(rec).search_logs(SCOPE, "status:error", start="now-1h", end="now", limit=10)
    assert isinstance(page, LogPage) and page.next_cursor == "c2"
    assert page.rows[0].message == "boom"
    request = rec.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.datadoghq.com/api/v2/logs/events/search"
    assert rec.sent()["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert request.headers["DD-API-KEY"] == API_KEY
    assert request.headers["DD-APPLICATION-KEY"] == APP_KEY


def test_search_logs_with_no_query_sends_the_scope_alone() -> None:
    rec = Recorder(body=LOG_BODY)
    _sync(rec).search_logs(SCOPE, "", start="now-1h", end="now")
    assert rec.sent()["filter"]["query"] == "service:shop-api"


def test_aggregate_logs_sends_the_composed_query() -> None:
    rec = Recorder(body=AGG_BODY)
    buckets = _sync(rec).aggregate_logs(
        SCOPE, "status:error", start="now-1h", end="now", group_by=["status"]
    )
    assert buckets == (Bucket(by={"status": "error"}, count=4),)
    assert str(rec.requests[0].url) == "https://api.datadoghq.com/api/v2/logs/analytics/aggregate"
    assert rec.sent()["filter"]["query"] == "(service:shop-api) AND (status:error)"


async def test_async_search_logs_sends_the_composed_query() -> None:
    rec = Recorder(body=LOG_BODY)
    page = await _async(rec).search_logs(SCOPE, "status:error", start="now-1h", end="now")
    assert page.next_cursor == "c2"
    assert rec.sent()["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert rec.requests[0].headers["DD-API-KEY"] == API_KEY


async def test_async_aggregate_logs_sends_the_composed_query() -> None:
    rec = Recorder(body=AGG_BODY)
    buckets = await _async(rec).aggregate_logs(
        SCOPE, "status:error", start="now-1h", end="now", group_by=["status"]
    )
    assert buckets[0].count == 4
    assert rec.sent()["filter"]["query"] == "(service:shop-api) AND (status:error)"


def test_the_site_picks_the_host() -> None:
    rec = Recorder(body=LOG_BODY)
    _sync(rec, site="datadoghq.eu").search_logs(SCOPE, "", start="now-1h", end="now")
    assert rec.requests[0].url.host == "api.datadoghq.eu"


# ── a refused query sends nothing ───────────────────────────────────────────


def test_a_refused_query_makes_no_request() -> None:
    rec = Recorder(body=LOG_BODY)
    client = _sync(rec)
    with pytest.raises(ScopeError):
        client.search_logs(SCOPE, ") OR (*", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        client.aggregate_logs(SCOPE, ") OR (*", start="now-1h", end="now", group_by=[])
    assert rec.requests == []


async def test_a_refused_query_makes_no_async_request() -> None:
    rec = Recorder(body=LOG_BODY)
    client = _async(rec)
    with pytest.raises(ScopeError):
        await client.search_logs(SCOPE, "*) OR (service:other", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        await client.aggregate_logs(SCOPE, '"open', start="now-1h", end="now", group_by=[])
    assert rec.requests == []


def test_a_no_access_scope_makes_no_request() -> None:
    from robothor.integrations.datadog.scope import NO_ACCESS_QUERY

    rec = Recorder(body=LOG_BODY)
    blocked = DatadogScope("acme-child-b", NO_ACCESS_QUERY, NO_ACCESS_QUERY, NO_ACCESS_QUERY, ())
    with pytest.raises(ScopeError):
        _sync(rec).search_logs(blocked, "status:error", start="now-1h", end="now")
    assert rec.requests == []


# ── errors ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "bad_query"),
        (401, "auth"),
        (403, "auth"),
        (429, "rate_limit"),
        (500, "network"),
        (503, "network"),
        (404, "unknown"),
        (302, "unknown"),
    ],
)
def test_a_status_maps_to_a_kind(status, kind) -> None:
    body = {"errors": [f"secret vendor detail {API_KEY}"]}
    with pytest.raises(DatadogError) as caught:
        _sync(Recorder(status=status, body=body)).validate()
    assert caught.value.kind == kind
    assert caught.value.status == status
    _assert_clean(caught.value)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connect failed to https://api.datadoghq.com"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_transport_failures_are_network(exc) -> None:
    with pytest.raises(DatadogError) as caught:
        _sync(Recorder(exc=exc)).search_logs(SCOPE, "", start="now-1h", end="now")
    assert caught.value.kind == "network"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    _assert_clean(caught.value)


async def test_async_errors_map_the_same() -> None:
    with pytest.raises(DatadogError) as caught:
        await _async(Recorder(status=403)).validate()
    assert caught.value.kind == "auth"
    with pytest.raises(DatadogError) as caught:
        await _async(Recorder(exc=httpx.ConnectError("x"))).validate()
    assert caught.value.kind == "network"
    _assert_clean(caught.value)


def test_a_body_that_is_not_json_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = DatadogClient(API_KEY, APP_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(DatadogError) as caught:
        client.search_logs(SCOPE, "", start="now-1h", end="now")
    assert caught.value.kind == "unknown"


def test_validate_passes_on_200() -> None:
    rec = Recorder(body={"valid": True})
    _sync(rec).validate()
    assert str(rec.requests[0].url) == "https://api.datadoghq.com/api/v1/validate"
    assert rec.requests[0].method == "GET"


def _assert_clean(exc: DatadogError) -> None:
    for text in (str(exc), repr(exc)):
        assert API_KEY not in text and APP_KEY not in text
        assert "datadoghq" not in text and "http" not in text.lower().replace("(http ", "")
        assert "vendor" not in text


# ── keys and sites ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", [DatadogClient, AsyncDatadogClient])
def test_repr_never_shows_the_keys(cls) -> None:
    client = cls(API_KEY, APP_KEY, site="datadoghq.eu")
    for text in (repr(client), str(client)):
        assert API_KEY not in text and APP_KEY not in text
        assert "datadoghq.eu" in text
    assert API_KEY not in repr(vars(client))


@pytest.mark.parametrize("cls", [DatadogClient, AsyncDatadogClient])
@pytest.mark.parametrize("site", ["example.com", "datadoghq.com.example.com", ""])
def test_an_unknown_site_is_not_configured(cls, site) -> None:
    with pytest.raises(DatadogError) as caught:
        cls(API_KEY, APP_KEY, site=site)
    assert caught.value.kind == "not_configured"
    _assert_clean(caught.value)


@pytest.mark.parametrize("cls", [DatadogClient, AsyncDatadogClient])
@pytest.mark.parametrize(("api", "app"), [("", APP_KEY), (API_KEY, ""), ("  ", "  ")])
def test_a_missing_key_is_not_configured(cls, api, app) -> None:
    with pytest.raises(DatadogError) as caught:
        cls(api, app)
    assert caught.value.kind == "not_configured"


def test_a_failure_logs_no_key_or_url(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DatadogError):
            _sync(Recorder(exc=httpx.ConnectError(f"boom {API_KEY}"))).validate()
    for record in caplog.records:
        text = record.getMessage()
        assert API_KEY not in text and "api.datadoghq.com" not in text


# ── from settings ───────────────────────────────────────────────────────────


@pytest.fixture
def settings(monkeypatch):
    from robothor.settings import reset_settings

    for name in ("ROBOTHOR_DATADOG_API_KEY", "ROBOTHOR_DATADOG_APP_KEY", "ROBOTHOR_DATADOG_SITE"):
        monkeypatch.delenv(name, raising=False)

    def _set(**pairs: str) -> None:
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    yield _set
    reset_settings()


def _no_vault(name: str, tenant_id: str) -> str | None:
    return None


def test_from_settings_without_keys_is_not_configured(settings) -> None:
    settings()
    with pytest.raises(DatadogError) as caught:
        client_from_settings(secret=_no_vault)
    assert caught.value.kind == "not_configured"
    with pytest.raises(DatadogError):
        async_client_from_settings(secret=_no_vault)


def test_from_settings_reads_keys_and_site(settings) -> None:
    settings(
        ROBOTHOR_DATADOG_API_KEY=API_KEY,
        ROBOTHOR_DATADOG_APP_KEY=APP_KEY,
        ROBOTHOR_DATADOG_SITE="us5.datadoghq.com",
    )
    client = client_from_settings(secret=_no_vault)
    assert client.site == "us5.datadoghq.com"
    assert isinstance(async_client_from_settings(secret=_no_vault), AsyncDatadogClient)


def test_from_settings_prefers_the_vault_and_reads_it_instance_wide(settings) -> None:
    from robothor.constants import DEFAULT_TENANT

    settings(ROBOTHOR_DATADOG_API_KEY="env-api", ROBOTHOR_DATADOG_APP_KEY="env-app")
    asked: list[tuple[str, str]] = []

    def vault(name: str, tenant_id: str) -> str | None:
        asked.append((name, tenant_id))
        return {"ROBOTHOR_DATADOG_API_KEY": "vault-api"}.get(name)

    rec = Recorder(body={"valid": True})
    client = client_from_settings(secret=vault, transport=httpx.MockTransport(rec))
    client.validate()
    assert rec.requests[0].headers["DD-API-KEY"] == "vault-api"
    assert rec.requests[0].headers["DD-APPLICATION-KEY"] == "env-app"
    assert {tenant for _name, tenant in asked} == {DEFAULT_TENANT}


def test_from_settings_with_an_unknown_site_is_not_configured(settings) -> None:
    settings(
        ROBOTHOR_DATADOG_API_KEY=API_KEY,
        ROBOTHOR_DATADOG_APP_KEY=APP_KEY,
        ROBOTHOR_DATADOG_SITE="logs.example.com",
    )
    with pytest.raises(DatadogError) as caught:
        client_from_settings(secret=_no_vault)
    assert caught.value.kind == "not_configured"
