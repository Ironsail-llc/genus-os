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
    MetricSeries,
    MonitorPage,
    SpanPage,
    async_client_from_settings,
    client_from_settings,
)
from robothor.integrations.datadog.scope import (
    NO_ACCESS_QUERY,
    NO_ACCESS_TAG,
    DatadogScope,
    ScopeError,
)

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


def test_the_application_key_probe_reads_no_tenant_data() -> None:
    rec = Recorder(body={"data": []})
    _sync(rec).validate_app_key()
    request = rec.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v2/logs/events/search"
    sent = json.loads(request.content)
    assert sent["filter"]["query"] == "genus_scope:no_access"
    assert sent["page"]["limit"] == 1


def test_a_rejected_application_key_is_an_auth_error() -> None:
    with pytest.raises(DatadogError) as caught:
        _sync(Recorder(status=403)).validate_app_key()
    assert caught.value.kind == "auth"


async def test_the_async_client_probes_the_application_key_too() -> None:
    rec = Recorder(body={"data": []})
    await _async(rec).validate_app_key()
    assert rec.requests[0].url.path == "/api/v2/logs/events/search"


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


# ── spans, traces, monitors, metrics: each bounded by its own part ──────────

ROOT = DatadogScope("acme-parent", "*", "*", "*", ())
NO_ACCESS = DatadogScope(
    "acme-child-b", "service:clinic-app", "service:clinic-app", NO_ACCESS_QUERY, (NO_ACCESS_TAG,)
)
SPAN_BODY = {
    "data": [
        {
            "id": "s1",
            "attributes": {
                "trace_id": "abc123",
                "span_id": "1",
                "service": "shop-api",
                "custom": {"duration": 5000},
            },
        }
    ],
    "meta": {"page": {"after": "s2"}},
}
SPAN_AGG_BODY = {"data": [{"attributes": {"by": {"service": "shop-api"}, "compute": {"c0": 7}}}]}
MONITOR_BODY = {
    "monitors": [{"id": 9, "name": "errors", "status": "Alert", "tags": ["team:shop"]}],
    "metadata": {"total_count": 1, "page": 0, "page_count": 1},
}
METRIC_BODY = {
    "status": "ok",
    "series": [
        {"metric": "m", "scope": "service:shop-api", "pointlist": [[1000, 2.0], [2000, None]]}
    ],
}


def _attrs(rec: Recorder, n: int = 0) -> dict:
    return rec.sent(n)["data"]["attributes"]


def _params(rec: Recorder, n: int = 0) -> dict[str, str]:
    return dict(rec.requests[n].url.params)


def test_search_spans_sends_the_composed_query() -> None:
    rec = Recorder(body=SPAN_BODY)
    page = _sync(rec).search_spans(SCOPE, "status:error", start="now-1h", end="now", limit=10)
    assert isinstance(page, SpanPage) and page.next_cursor == "s2"
    assert page.rows[0].trace_id == "abc123" and page.rows[0].duration_ns == 5000
    request = rec.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.datadoghq.com/api/v2/spans/events/search"
    assert request.headers["DD-API-KEY"] == API_KEY
    attrs = _attrs(rec)
    assert attrs["filter"] == {
        "query": "(service:shop-api) AND (status:error)",
        "from": "now-1h",
        "to": "now",
    }
    assert attrs["page"] == {"limit": 10}


def test_spans_are_bounded_by_the_spans_filter_not_the_logs_one() -> None:
    rec = Recorder(body=SPAN_BODY)
    scope = DatadogScope("acme-child-a", "service:shop-api", "service:shop-api env:prod", "*", ())
    _sync(rec).search_spans(scope, "", start="now-1h", end="now")
    assert _attrs(rec)["filter"]["query"] == "service:shop-api env:prod"


def test_aggregate_spans_sends_the_composed_query() -> None:
    rec = Recorder(body=SPAN_AGG_BODY)
    buckets = _sync(rec).aggregate_spans(
        SCOPE, "status:error", start="now-1h", end="now", group_by=["service"]
    )
    assert buckets == (Bucket(by={"service": "shop-api"}, count=7),)
    assert str(rec.requests[0].url) == "https://api.datadoghq.com/api/v2/spans/analytics/aggregate"
    assert _attrs(rec)["filter"]["query"] == "(service:shop-api) AND (status:error)"


def test_get_trace_is_a_scoped_span_search_for_the_trace() -> None:
    rec = Recorder(body=SPAN_BODY)
    spans = _sync(rec).get_trace(SCOPE, "abc123", start="now-1d", end="now")
    assert spans and spans[0].trace_id == "abc123"
    assert str(rec.requests[0].url) == "https://api.datadoghq.com/api/v2/spans/events/search"
    attrs = _attrs(rec)
    assert attrs["filter"]["query"] == "(service:shop-api) AND (trace_id:abc123)"
    assert attrs["page"]["limit"] == 200
    assert attrs["sort"] == "timestamp"


def test_a_trace_from_another_tenant_reads_as_empty() -> None:
    rec = Recorder(body={"data": [], "meta": {}})
    assert _sync(rec).get_trace(SCOPE, "abc123", start="now-1d", end="now") == ()
    assert len(rec.requests) == 1


@pytest.mark.parametrize("trace_id", ["abc) OR (service:other", "abc OR *", "", "trace_id:1"])
def test_a_malformed_trace_id_makes_no_request(trace_id) -> None:
    rec = Recorder(body=SPAN_BODY)
    with pytest.raises(ScopeError):
        _sync(rec).get_trace(SCOPE, trace_id, start="now-1d", end="now")
    assert rec.requests == []


def test_search_monitors_sends_the_composed_query() -> None:
    rec = Recorder(body=MONITOR_BODY)
    page = _sync(rec).search_monitors(SCOPE, "status:alert", limit=20, page=1)
    assert isinstance(page, MonitorPage) and page.total_count == 1
    assert page.rows[0].id == "9" and page.rows[0].status == "Alert"
    request = rec.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/monitor/search"
    assert request.url.host == "api.datadoghq.com"
    assert _params(rec) == {
        "query": "(team:shop) AND (status:alert)",
        "page": "1",
        "per_page": "20",
    }
    assert request.headers["DD-APPLICATION-KEY"] == APP_KEY


def test_search_monitors_with_no_query_sends_the_scope_alone() -> None:
    rec = Recorder(body=MONITOR_BODY)
    _sync(rec).search_monitors(SCOPE)
    assert _params(rec)["query"] == "team:shop"


def test_query_metric_sends_the_built_query_and_epoch_window() -> None:
    rec = Recorder(body=METRIC_BODY)
    series = _sync(rec).query_metric(
        SCOPE,
        "trace.http.request.hits",
        aggregator="sum",
        tags=("env:prod",),
        group_by=("resource_name",),
        start="2026-11-25T09:00:00Z",
        end="2026-11-25T10:00:00Z",
        as_count=True,
    )
    assert series == (
        MetricSeries(metric="m", scope="service:shop-api", tags=(), points=((1000, 2.0),)),
    )
    request = rec.requests[0]
    assert request.method == "GET" and request.url.path == "/api/v1/query"
    assert _params(rec) == {
        "from": "1795597200",
        "to": "1795600800",
        "query": "sum:trace.http.request.hits{service:shop-api,env:prod} by {resource_name}"
        ".as_count()",
    }


def test_query_metric_resolves_a_relative_window() -> None:
    rec = Recorder(body=METRIC_BODY)
    _sync(rec).query_metric(SCOPE, "m", start="now-1h", end="now")
    params = _params(rec)
    assert int(params["to"]) - int(params["from"]) == 3600
    assert params["query"] == "avg:m{service:shop-api}"


@pytest.mark.parametrize("start", ["yesterday", "now-1w", "1h"])
def test_query_metric_with_a_bad_window_is_bad_query_and_sends_nothing(start) -> None:
    rec = Recorder(body=METRIC_BODY)
    with pytest.raises(DatadogError) as caught:
        _sync(rec).query_metric(SCOPE, "m", start=start, end="now")
    assert caught.value.kind == "bad_query"
    assert rec.requests == []
    _assert_clean(caught.value)


def test_a_metric_error_status_in_a_200_body_is_bad_query() -> None:
    rec = Recorder(body={"status": "error", "error": f"Error parsing query {API_KEY}"})
    with pytest.raises(DatadogError) as caught:
        _sync(rec).query_metric(SCOPE, "m", start="now-1h", end="now")
    assert caught.value.kind == "bad_query"
    _assert_clean(caught.value)


def test_the_scope_tags_are_present_whatever_the_caller_passes() -> None:
    rec = Recorder(body=METRIC_BODY)
    client = _sync(rec)
    for tags in [(), ("service:clinic-app",), ("env:prod", "host:web-1")]:
        client.query_metric(SCOPE, "m", tags=tags, start="now-1h", end="now")
    for n in range(3):
        assert _params(rec, n)["query"].startswith("avg:m{service:shop-api")


@pytest.mark.parametrize(
    ("metric", "tags", "group_by"),
    [
        ("m}", (), ()),
        ("m{*},avg:other", (), ()),
        ("m", ("host:a}",), ()),
        ("m", ("host:a,service:other",), ()),
        ("m", ("host:a OR service:b",), ()),
        ("m", ('host:"a"',), ()),
        ("m", (), ("host}",)),
        ("m", (), ("host,env",)),
        ("m", (), ("host env",)),
    ],
)
def test_a_metric_injection_makes_no_request(metric, tags, group_by) -> None:
    rec = Recorder(body=METRIC_BODY)
    with pytest.raises(ScopeError):
        _sync(rec).query_metric(
            SCOPE, metric, tags=tags, group_by=group_by, start="now-1h", end="now"
        )
    assert rec.requests == []


# ── refusals and no access send nothing ─────────────────────────────────────


def test_a_refused_query_makes_no_request_on_any_new_path() -> None:
    rec = Recorder(body=SPAN_BODY)
    client = _sync(rec)
    with pytest.raises(ScopeError):
        client.search_spans(SCOPE, ") OR (*", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        client.aggregate_spans(SCOPE, '"open', start="now-1h", end="now", group_by=[])
    with pytest.raises(ScopeError):
        client.search_monitors(SCOPE, "*) OR (team:other")
    assert rec.requests == []


def test_a_no_access_monitor_or_metric_scope_makes_no_request() -> None:
    rec = Recorder(body=MONITOR_BODY)
    client = _sync(rec)
    with pytest.raises(ScopeError):
        client.search_monitors(NO_ACCESS, "status:alert")
    with pytest.raises(ScopeError):
        client.search_monitors(NO_ACCESS)
    with pytest.raises(ScopeError):
        client.query_metric(NO_ACCESS, "m", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        client.query_metric(NO_ACCESS, "m", tags=("service:clinic-app",), start="now", end="now")
    assert rec.requests == []


def test_a_no_access_spans_scope_makes_no_request() -> None:
    rec = Recorder(body=SPAN_BODY)
    blocked = DatadogScope("acme-child-b", "*", NO_ACCESS_QUERY, NO_ACCESS_QUERY, (NO_ACCESS_TAG,))
    client = _sync(rec)
    with pytest.raises(ScopeError):
        client.search_spans(blocked, "", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        client.get_trace(blocked, "abc123", start="now-1h", end="now")
    assert rec.requests == []


def test_a_declared_entry_without_spans_reads_no_spans(monkeypatch) -> None:
    """The logs filter is never reused for spans: no ``spans`` key is no access."""
    from robothor.integrations.datadog.scope import scope_for
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps({"acme-child-a": {"logs": "*"}}))
    reset_settings()
    try:
        scope = scope_for("acme-child-a")
    finally:
        reset_settings()
    assert scope is not None
    rec = Recorder(body=SPAN_BODY)
    client = _sync(rec)
    with pytest.raises(ScopeError):
        client.search_spans(scope, "", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        client.aggregate_spans(scope, "", start="now-1h", end="now", group_by=["service"])
    with pytest.raises(ScopeError):
        client.get_trace(scope, "abc123", start="now-1h", end="now")
    assert rec.requests == []


# ── the root scope passes the caller's query through ───────────────────────


def test_the_root_scope_passes_each_query_through() -> None:
    rec = Recorder(body={})
    client = _sync(rec)
    client.search_spans(ROOT, "service:clinic-app", start="now-1h", end="now")
    client.aggregate_spans(ROOT, "service:clinic-app", start="now-1h", end="now", group_by=[])
    client.get_trace(ROOT, "abc123", start="now-1h", end="now")
    client.search_monitors(ROOT, "status:alert")
    client.query_metric(ROOT, "m", tags=("host:web-1",), start="now-1h", end="now")
    client.query_metric(ROOT, "m", start="now-1h", end="now")
    assert _attrs(rec, 0)["filter"]["query"] == "service:clinic-app"
    assert _attrs(rec, 1)["filter"]["query"] == "service:clinic-app"
    assert _attrs(rec, 2)["filter"]["query"] == "trace_id:abc123"
    assert _params(rec, 3)["query"] == "status:alert"
    assert _params(rec, 4)["query"] == "avg:m{host:web-1}"
    assert _params(rec, 5)["query"] == "avg:m{*}"


def test_the_root_scope_with_no_monitor_query_lists_every_monitor() -> None:
    rec = Recorder(body=MONITOR_BODY)
    _sync(rec).search_monitors(ROOT)
    assert _params(rec)["query"] == ""


# ── errors and odd bodies on the new paths ──────────────────────────────────

_CALLS = [
    lambda c: c.search_spans(SCOPE, "", start="now-1h", end="now"),
    lambda c: c.aggregate_spans(SCOPE, "", start="now-1h", end="now", group_by=["service"]),
    lambda c: c.get_trace(SCOPE, "abc123", start="now-1h", end="now"),
    lambda c: c.search_monitors(SCOPE),
    lambda c: c.query_metric(SCOPE, "m", start="now-1h", end="now"),
]


@pytest.mark.parametrize("call", _CALLS)
@pytest.mark.parametrize(
    ("status", "kind"), [(400, "bad_query"), (403, "auth"), (429, "rate_limit"), (502, "network")]
)
def test_each_new_path_maps_errors_to_kinds(call, status, kind) -> None:
    with pytest.raises(DatadogError) as caught:
        call(_sync(Recorder(status=status, body={"errors": [f"vendor {API_KEY}"]})))
    assert (caught.value.kind, caught.value.status) == (kind, status)
    _assert_clean(caught.value)


@pytest.mark.parametrize("call", _CALLS)
def test_each_new_path_turns_a_transport_failure_into_network(call) -> None:
    with pytest.raises(DatadogError) as caught:
        call(_sync(Recorder(exc=httpx.ConnectError(f"to https://api.datadoghq.com {API_KEY}"))))
    assert caught.value.kind == "network" and caught.value.__suppress_context__
    _assert_clean(caught.value)


@pytest.mark.parametrize("call", _CALLS)
def test_each_new_path_reads_an_empty_body_as_empty(call) -> None:
    result = call(_sync(Recorder(body={})))
    if isinstance(result, SpanPage):
        assert result == SpanPage(rows=(), next_cursor=None)
    elif isinstance(result, MonitorPage):
        assert result.rows == () and result.total_count == 0
    else:
        assert result == ()


# ── the async client, the same paths ────────────────────────────────────────


async def test_async_new_paths_send_the_bounded_queries() -> None:
    rec = Recorder(body={})
    client = _async(rec)
    await client.search_spans(SCOPE, "status:error", start="now-1h", end="now")
    await client.aggregate_spans(SCOPE, "status:error", start="now-1h", end="now", group_by=[])
    await client.get_trace(SCOPE, "abc123", start="now-1h", end="now")
    await client.search_monitors(SCOPE, "status:alert")
    await client.query_metric(SCOPE, "m", tags=("env:prod",), start="now-1h", end="now")
    assert _attrs(rec, 0)["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert _attrs(rec, 1)["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert _attrs(rec, 2)["filter"]["query"] == "(service:shop-api) AND (trace_id:abc123)"
    assert _params(rec, 3)["query"] == "(team:shop) AND (status:alert)"
    assert _params(rec, 4)["query"] == "avg:m{service:shop-api,env:prod}"
    assert rec.requests[4].headers["DD-API-KEY"] == API_KEY


async def test_async_new_paths_parse_their_bodies() -> None:
    assert (
        await _async(Recorder(body=SPAN_BODY)).search_spans(SCOPE, "", start="now-1h", end="now")
    ).next_cursor == "s2"
    buckets = await _async(Recorder(body=SPAN_AGG_BODY)).aggregate_spans(
        SCOPE, "", start="now-1h", end="now", group_by=["service"]
    )
    assert buckets[0].count == 7
    trace = await _async(Recorder(body=SPAN_BODY)).get_trace(
        SCOPE, "abc123", start="now-1h", end="now"
    )
    assert trace[0].span_id == "1"
    monitors = await _async(Recorder(body=MONITOR_BODY)).search_monitors(SCOPE)
    assert monitors.rows[0].name == "errors"
    series = await _async(Recorder(body=METRIC_BODY)).query_metric(
        SCOPE, "m", start="now-1h", end="now"
    )
    assert series[0].points == ((1000, 2.0),)


async def test_async_refusals_and_no_access_send_nothing() -> None:
    rec = Recorder(body={})
    client = _async(rec)
    with pytest.raises(ScopeError):
        await client.search_spans(SCOPE, ") OR (*", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        await client.aggregate_spans(SCOPE, "(", start="now-1h", end="now", group_by=[])
    with pytest.raises(ScopeError):
        await client.get_trace(SCOPE, "abc OR *", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        await client.search_monitors(NO_ACCESS)
    with pytest.raises(ScopeError):
        await client.query_metric(NO_ACCESS, "m", start="now-1h", end="now")
    with pytest.raises(ScopeError):
        await client.query_metric(SCOPE, "m", tags=("a}",), start="now-1h", end="now")
    with pytest.raises(DatadogError) as caught:
        await client.query_metric(SCOPE, "m", start="later", end="now")
    assert caught.value.kind == "bad_query"
    assert rec.requests == []


async def test_async_new_paths_map_errors() -> None:
    with pytest.raises(DatadogError) as caught:
        await _async(Recorder(status=429)).search_monitors(SCOPE)
    assert caught.value.kind == "rate_limit"
    with pytest.raises(DatadogError) as caught:
        await _async(Recorder(exc=httpx.ReadTimeout("t"))).query_metric(
            SCOPE, "m", start="now-1h", end="now"
        )
    assert caught.value.kind == "network"
    with pytest.raises(DatadogError) as caught:
        await _async(Recorder(body={"status": "error"})).query_metric(
            SCOPE, "m", start="now-1h", end="now"
        )
    assert caught.value.kind == "bad_query"


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
