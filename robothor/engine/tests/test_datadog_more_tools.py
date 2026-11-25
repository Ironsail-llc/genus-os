"""Spans, one trace, monitors and metrics: a tenant reads its own, read-only.

``datadog_spans_search``, ``datadog_trace_get``, ``datadog_monitors_list`` and
``datadog_metrics_query`` join the log tools in the Datadog family. As there,
the scope is the run's tenant (``ctx.tenant_id``) and nothing else, and no test
reaches the network: ``datadog_more._client_factory`` is replaced with a client
over an in-process fake answering in Datadog's own response shapes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.models import TriggerType
from robothor.engine.tools.dispatch import ToolContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
UNCONFIGURED = "test-tenant"
NO_SPANS = "acme-child-c"
SPANS, TRACE = "datadog_spans_search", "datadog_trace_get"
MONITORS, METRICS = "datadog_monitors_list", "datadog_metrics_query"
TOOLS = (SPANS, TRACE, MONITORS, METRICS)
API_KEY, APP_KEY = "dd-api-key-0123456789", "dd-app-key-9876543210"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"

SCOPES = {
    PARENT: {"logs": "*", "spans": "*", "monitors": "*", "metric_tags": []},
    CHILD_A: {
        "logs": "service:shop-api",
        "spans": "service:shop-api",
        "monitors": 'tag:"service:shop-api"',
        "metric_tags": ["service:shop-api"],
    },
    # Spans default to the log filter; monitors and metrics to no access.
    CHILD_B: {"logs": "service:clinic-app"},
    # Logs, but no spans: the span part itself is refused.
    NO_SPANS: {"logs": "service:shop-api", "spans": 'tag:"genus_scope:no_access"'},
}

#: The fixed "now" every test runs at, and the windows it resolves to.
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
NOW_ISO, HOUR_AGO, DAY_AGO = (
    "2026-11-25T12:00:00.000Z",
    "2026-11-25T11:00:00.000Z",
    "2026-11-24T12:00:00.000Z",
)

#: Arguments each tool accepts with no other input, for the parametrized cases.
MINIMAL: dict[str, dict[str, Any]] = {
    SPANS: {"query": "status:error"},
    TRACE: {"trace_id": TRACE_ID},
    MONITORS: {"query": "status:alert"},
    METRICS: {"metric": "trace.http.request.hits"},
}


# ── a fake Datadog ─────────────────────────────────────────────────────────


def _span(i: int, resource: str = "", **over: Any) -> dict[str, Any]:
    attributes = {
        "trace_id": TRACE_ID,
        "span_id": f"{1000 + i}",
        "parent_id": "0" if i == 0 else "1000",
        "service": "shop-api",
        "resource_name": resource or f"GET /checkout/{i}",
        "operation_name": "http.request",
        "start_timestamp": f"2026-11-25T10:{i // 60:02d}:{i % 60:02d}.000Z",
        "end_timestamp": f"2026-11-25T10:{i // 60:02d}:{i % 60:02d}.250Z",
        "status": "error" if i % 2 else "ok",
        "env": "prod",
        "tags": ["env:prod", "service:shop-api"],
        "custom": {"duration": 250_000_000, "http": {"status_code": 500}},
    }
    attributes.update(over)
    return {"id": f"s{i}", "type": "spans", "attributes": attributes}


def _monitor(i: int, name: str = "", **over: Any) -> dict[str, Any]:
    raw = {
        "id": 100 + i,
        "name": name or f"shop-api error rate {i}",
        "status": "Alert" if i == 0 else "OK",
        "type": "query alert",
        "tags": ["service:shop-api", "team:checkout"],
        "last_triggered_ts": 1795597200,
    }
    raw.update(over)
    return raw


class FakeDatadog:
    """Answers the span, monitor and metric endpoints and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.make_span = lambda i: _span(i)
        #: The spans a span search returns; None = `page.limit` made by make_span.
        self.spans: list[dict[str, Any]] | None = None
        self.monitors: list[dict[str, Any]] = [_monitor(0), _monitor(1)]
        self.monitor_meta = {"total_count": 2, "page": 0, "page_count": 1}
        self.series: list[dict[str, Any]] = [
            {
                "metric": "trace.http.request.hits",
                "scope": "service:shop-api,host:web-1",
                "tag_set": ["service:shop-api", "host:web-1"],
                "pointlist": [[1795597200000.0, 3.0], [1795597260000.0, 5.0]],
            }
        ]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]

    def params(self, n: int = 0) -> dict[str, str]:
        return dict(self.requests[n].url.params)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["vendor detail with secrets"]})
        path = request.url.path
        if path == "/api/v2/spans/events/search":
            limit = json.loads(request.content)["data"]["attributes"]["page"]["limit"]
            rows = (
                self.spans if self.spans is not None else [self.make_span(i) for i in range(limit)]
            )
            meta = {"page": {"after": f"after-{limit}"}} if self.spans is None else {}
            return httpx.Response(200, json={"data": rows, "meta": meta})
        if path == "/api/v1/monitor/search":
            return httpx.Response(
                200, json={"monitors": self.monitors, "metadata": self.monitor_meta}
            )
        if path == "/api/v1/query":
            return httpx.Response(200, json={"status": "ok", "series": self.series})
        return httpx.Response(404)


@pytest.fixture
def scopes(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", json.dumps(SCOPES))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    from robothor.engine.tools.handlers import datadog_more

    monkeypatch.setattr(datadog_more, "_now", lambda: NOW, raising=False)


@pytest.fixture
def dd(monkeypatch, scopes):
    from robothor.engine.tools.handlers import datadog_more
    from robothor.integrations.datadog.client import AsyncDatadogClient

    fake = FakeDatadog()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(
        datadog_more,
        "_client_factory",
        lambda: AsyncDatadogClient(API_KEY, APP_KEY, transport=transport),
    )
    return fake


@pytest.fixture
def writer():
    """The engine's event writer, captured: the audit rows the tools queue."""
    fake = MagicMock()
    with patch("robothor.engine.event_writer.get_event_writer", return_value=fake):
        yield fake


def _ctx(tenant: str = CHILD_A) -> ToolContext:
    return ToolContext(agent_id="ops-agent", run_id="run-1", tenant_id=tenant, user_id="alice")


def _run(name: str, args: dict[str, Any], ctx: ToolContext | None = None) -> dict[str, Any]:
    from robothor.engine.tools.handlers import datadog_more

    return asyncio.run(datadog_more.HANDLERS[name](args, ctx or _ctx()))


def _attrs(fake: FakeDatadog, n: int = 0) -> dict[str, Any]:
    return fake.bodies()[n]["data"]["attributes"]


# ── datadog_spans_search ───────────────────────────────────────────────────


def test_spans_search_sends_the_tenants_span_scope_with_the_query(dd, writer):
    result = _run(SPANS, {"query": "status:error"})

    assert "error" not in result
    attrs = _attrs(dd)
    assert attrs["filter"]["query"] == "(service:shop-api) AND (status:error)"
    # The relative default is resolved once, so the window handed back (and
    # paged with) is the one that was searched.
    assert (attrs["filter"]["from"], attrs["filter"]["to"]) == (HOUR_AGO, NOW_ISO)
    assert result["window"] == {"start": HOUR_AGO, "end": NOW_ISO}
    assert attrs["sort"] == "-timestamp"
    assert result["scope"] == CHILD_A


def test_spans_search_passes_window_limit_cursor_and_order(dd, writer):
    _run(
        SPANS,
        {
            "query": "service:shop-api",
            "start": "2026-11-25T09:00:00Z",
            "end": "2026-11-25T10:00:00Z",
            "limit": 7,
            "cursor": "abc",
            "newest_first": False,
        },
    )

    attrs = _attrs(dd)
    assert (attrs["filter"]["from"], attrs["filter"]["to"]) == (
        "2026-11-25T09:00:00Z",
        "2026-11-25T10:00:00Z",
    )
    assert attrs["page"] == {"limit": 7, "cursor": "abc"}
    assert attrs["sort"] == "timestamp"


def test_span_rows_carry_duration_in_milliseconds_and_the_ids(dd, writer):
    result = _run(SPANS, {"limit": 2})

    assert result["next_cursor"] == "after-2"
    assert result["rows_returned"] == 2
    row = result["rows"][1]
    assert row["time"] == "2026-11-25T10:00:01.000Z"
    assert (row["service"], row["resource"], row["operation"]) == (
        "shop-api",
        "GET /checkout/1",
        "http.request",
    )
    assert row["duration_ms"] == 250.0
    assert (row["status"], row["error"]) == ("error", True)
    assert (row["trace_id"], row["span_id"], row["env"]) == (TRACE_ID, "1001", "prod")
    assert result["rows"][0]["error"] is False


def test_span_resource_names_are_masked(dd, writer):
    dd.make_span = lambda i: _span(i, "GET /users/alice@example.com?phone=+1 415 555 0100")

    result = _run(SPANS, {"limit": 1})

    text = json.dumps(result)
    assert "alice@example.com" not in text and "555 0100" not in text
    assert "<email>" in result["rows"][0]["resource"]


def test_an_oversized_span_page_is_fitted_with_a_cursor_that_continues(dd, writer):
    from robothor.engine.tools.handlers import datadog_more

    dd.make_span = lambda i: _span(i, "x" * 3000)

    result = _run(SPANS, {"limit": 300})

    assert result["truncated"] is True
    kept = len(result["rows"])
    assert 1 <= kept < 300
    assert result["rows_returned"] == kept
    assert str(kept) in result["note"]
    assert result["next_cursor"] == f"after-{kept}"
    assert len(json.dumps(result)) <= datadog_more.RESULT_BUDGET_CHARS


def test_a_span_page_that_fits_is_not_truncated(dd, writer):
    result = _run(SPANS, {"limit": 3})

    assert result["truncated"] is False
    assert "note" not in result
    assert len(dd.requests) == 1


def test_a_parent_with_every_span_sends_the_query_alone(dd, writer):
    _run(SPANS, {"query": "status:error"}, _ctx(PARENT))

    assert _attrs(dd)["filter"]["query"] == "status:error"


def test_a_sibling_without_a_span_filter_reads_with_its_log_filter(dd, writer):
    _run(SPANS, {"query": "status:error"}, _ctx(CHILD_B))

    assert _attrs(dd)["filter"]["query"] == "(service:clinic-app) AND (status:error)"


# ── datadog_trace_get ──────────────────────────────────────────────────────


def test_trace_get_searches_the_tenants_spans_for_the_id_over_a_day(dd, writer):
    dd.spans = [_span(0)]

    _run(TRACE, {"trace_id": TRACE_ID})

    attrs = _attrs(dd)
    assert attrs["filter"]["query"] == f"(service:shop-api) AND (trace_id:{TRACE_ID})"
    assert (attrs["filter"]["from"], attrs["filter"]["to"]) == (DAY_AGO, NOW_ISO)


def test_trace_get_returns_the_spans_by_start_time_with_parent_ids(dd, writer):
    dd.spans = [_span(2), _span(0), _span(1)]

    result = _run(TRACE, {"trace_id": TRACE_ID, "start": "now-2d", "end": "now-1d"})

    assert result["found"] is True
    assert result["trace_id"] == TRACE_ID
    assert [s["span_id"] for s in result["spans"]] == ["1000", "1001", "1002"]
    assert [s["parent_id"] for s in result["spans"]] == ["0", "1000", "1000"]
    assert result["spans_returned"] == 3
    attrs = _attrs(dd)
    assert (attrs["filter"]["from"], attrs["filter"]["to"]) == ("2026-11-23T12:00:00.000Z", DAY_AGO)
    assert result["window"] == {"start": "2026-11-23T12:00:00.000Z", "end": DAY_AGO}


def test_a_trace_outside_the_scope_reads_as_not_found_never_as_an_error(dd, writer):
    dd.spans = []

    result = _run(TRACE, {"trace_id": TRACE_ID})

    assert result["found"] is False
    assert "error" not in result and "refused" not in result
    assert "not found in your organization's traces" in result["note"]
    assert result["spans"] == []


@pytest.mark.parametrize(
    "trace_id", ["", "abc OR *", "abc) OR (service:clinic-app", "abc\nservice:x", "g" * 8]
)
def test_a_malformed_trace_id_is_refused_with_no_request(dd, writer, trace_id):
    result = _run(TRACE, {"trace_id": trace_id})

    assert result["refused"] is True
    assert dd.requests == []


def test_trace_span_resource_names_are_masked(dd, writer):
    dd.spans = [_span(0, "POST /login bob@example.com")]

    result = _run(TRACE, {"trace_id": TRACE_ID})

    assert "bob@example.com" not in json.dumps(result)


def test_an_oversized_trace_keeps_the_earliest_spans_and_says_truncated(dd, writer):
    from robothor.engine.tools.handlers import datadog_more

    dd.spans = [_span(i, "y" * 3000) for i in range(200)]

    result = _run(TRACE, {"trace_id": TRACE_ID})

    assert result["truncated"] is True
    kept = result["spans_returned"]
    assert 1 <= kept < 200
    assert result["spans_total"] == 200
    assert [s["span_id"] for s in result["spans"]] == [f"{1000 + i}" for i in range(kept)]
    assert str(kept) in result["note"]
    assert len(json.dumps(result)) <= datadog_more.RESULT_BUDGET_CHARS


# ── datadog_monitors_list ──────────────────────────────────────────────────


def test_monitors_list_sends_the_tenants_monitor_scope_with_the_query(dd, writer):
    result = _run(MONITORS, {"query": "status:alert", "limit": 20, "page": 1})

    assert "error" not in result
    params = dd.params()
    assert dd.requests[0].url.path == "/api/v1/monitor/search"
    assert params["query"] == '(tag:"service:shop-api") AND (status:alert)'
    assert (params["page"], params["per_page"]) == ("1", "20")
    assert result["scope"] == CHILD_A


def test_only_firing_adds_the_alert_warn_and_no_data_filter(dd, writer):
    _run(MONITORS, {"only_firing": True})

    assert dd.params()["query"] == (
        '(tag:"service:shop-api") AND (status:alert OR status:warn OR status:"no data")'
    )


def test_only_firing_with_a_query_keeps_both(dd, writer):
    _run(MONITORS, {"query": "team:checkout", "only_firing": True})

    assert dd.params()["query"] == (
        '(tag:"service:shop-api") AND '
        '((team:checkout) AND (status:alert OR status:warn OR status:"no data"))'
    )


def test_monitor_rows_carry_status_type_tags_and_last_triggered(dd, writer):
    result = _run(MONITORS, {})

    first = result["monitors"][0]
    assert first["id"] == "100"
    assert first["name"] == "shop-api error rate 0"
    assert (first["status"], first["type"]) == ("Alert", "query alert")
    assert first["tags"] == ["service:shop-api", "team:checkout"]
    assert first["last_triggered"].startswith("2026-")
    assert (result["total_count"], result["page"], result["page_count"]) == (2, 0, 1)


def test_monitor_names_and_tags_are_masked(dd, writer):
    dd.monitors = [
        _monitor(0, "Login failures for alice@example.com", tags=["owner:bob@example.com"])
    ]

    result = _run(MONITORS, {})

    text = json.dumps(result)
    assert "alice@example.com" not in text and "bob@example.com" not in text
    assert "<email>" in result["monitors"][0]["name"]


def test_more_monitor_pages_are_named(dd, writer):
    dd.monitor_meta = {"total_count": 120, "page": 0, "page_count": 3}

    result = _run(MONITORS, {})

    assert result["next_page"] == 1


def test_an_oversized_monitor_page_is_fitted(dd, writer):
    from robothor.engine.tools.handlers import datadog_more

    dd.monitors = [_monitor(i, "z" * 2000) for i in range(300)]

    result = _run(MONITORS, {})

    assert result["truncated"] is True
    assert 1 <= len(result["monitors"]) < 300
    assert len(json.dumps(result)) <= datadog_more.RESULT_BUDGET_CHARS


def test_a_tenant_without_monitor_access_is_refused_with_no_request(dd, writer):
    result = _run(MONITORS, {}, _ctx(CHILD_B))

    assert result["refused"] is True
    assert dd.requests == []


def test_a_parent_with_every_monitor_lists_them_all(dd, writer):
    _run(MONITORS, {}, _ctx(PARENT))

    assert dd.params()["query"] == ""


# ── datadog_metrics_query ──────────────────────────────────────────────────


def test_metrics_query_is_built_from_parts_inside_the_scope(dd, writer):
    result = _run(
        METRICS,
        {
            "metric": "trace.http.request.hits",
            "aggregator": "sum",
            "tags": ["env:prod"],
            "group_by": ["host"],
            "as_count": True,
            "start": "now-4h",
        },
    )

    assert "error" not in result
    params = dd.params()
    assert dd.requests[0].url.path == "/api/v1/query"
    assert params["query"] == (
        "sum:trace.http.request.hits{service:shop-api,env:prod} by {host}.as_count()"
    )
    assert int(params["to"]) - int(params["from"]) == 4 * 3600
    assert result["window"] == {"start": "2026-11-25T08:00:00.000Z", "end": NOW_ISO}
    assert result["scope"] == CHILD_A


def test_metrics_query_defaults_to_avg_over_the_last_hour(dd, writer):
    _run(METRICS, {"metric": "system.cpu.user"})

    params = dd.params()
    assert params["query"] == "avg:system.cpu.user{service:shop-api}"
    assert int(params["to"]) - int(params["from"]) == 3600


@pytest.mark.parametrize("tags", [[], ["service:clinic-app"], ["env:prod", "host:web-1"]])
def test_the_scope_tags_are_always_in_the_built_query(dd, writer, tags):
    _run(METRICS, {"metric": "m", "tags": tags})

    assert dd.params()["query"].startswith("avg:m{service:shop-api")


def test_series_carry_their_group_tags_and_points(dd, writer):
    result = _run(METRICS, {"metric": "trace.http.request.hits", "group_by": ["host"]})

    (series,) = result["series"]
    assert series["tags"] == ["service:shop-api", "host:web-1"]
    assert series["points"] == [[1795597200000, 3.0], [1795597260000, 5.0]]
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "args",
    [
        {"metric": "m{*},avg:other"},
        {"metric": "m}"},
        {"metric": "m", "aggregator": "avg:x{*}"},
        {"metric": "m", "aggregator": "count"},
        {"metric": "m", "tags": ["host:a}"]},
        {"metric": "m", "tags": ["host:a,service:clinic-app"]},
        {"metric": "m", "tags": ["host:a OR service:b"]},
        {"metric": "m", "tags": ['host:"a"']},
        {"metric": "m", "group_by": ["host}"]},
        {"metric": "m", "group_by": ["host,env"]},
        {"metric": "m", "group_by": ["host env"]},
        {"metric": ""},
    ],
)
def test_a_metric_injection_in_any_argument_is_refused_with_no_request(dd, writer, args):
    result = _run(METRICS, args)

    assert result["refused"] is True
    assert dd.requests == []


@pytest.mark.parametrize("start", ["yesterday", "now-1y", "1h"])
def test_a_metric_window_it_cannot_read_is_bad_query_with_no_request(dd, writer, start):
    result = _run(METRICS, {"metric": "m", "start": start})

    assert result["kind"] == "bad_query"
    assert dd.requests == []


def test_a_tenant_without_metric_access_is_refused_with_no_request(dd, writer):
    result = _run(METRICS, {"metric": "m"}, _ctx(CHILD_B))

    assert result["refused"] is True
    assert dd.requests == []


def test_a_parent_with_every_metric_queries_star(dd, writer):
    _run(METRICS, {"metric": "m"}, _ctx(PARENT))

    assert dd.params()["query"] == "avg:m{*}"


def test_long_series_are_down_sampled_keeping_first_last_min_and_max(dd, writer):
    from robothor.engine.tools.handlers import datadog_more

    points = [[1795597200000 + i * 1000, float(i % 97)] for i in range(5000)]
    points[1234][1] = -50.0
    points[3456][1] = 999.0
    dd.series = [
        {"metric": "m", "scope": "host:web-1", "tag_set": ["host:web-1"], "pointlist": points}
    ]

    result = _run(METRICS, {"metric": "m"})

    assert result["truncated"] is True
    (series,) = result["series"]
    kept = series["points"]
    assert 4 <= len(kept) < 5000
    assert series["points_total"] == 5000
    assert kept[0] == points[0] and kept[-1] == points[-1]
    assert [points[1234][0], -50.0] in kept and [points[3456][0], 999.0] in kept
    assert [p[0] for p in kept] == sorted(p[0] for p in kept)
    assert len(json.dumps(result)) <= datadog_more.RESULT_BUDGET_CHARS


def test_series_group_tags_are_masked(dd, writer):
    dd.series[0]["tag_set"] = ["user:alice@example.com"]
    dd.series[0]["scope"] = "user:alice@example.com"

    result = _run(METRICS, {"metric": "m"})

    assert "alice@example.com" not in json.dumps(result)


# ── every tool: refusals, errors, audit ────────────────────────────────────


@pytest.mark.parametrize("name", TOOLS)
def test_a_tenant_with_no_scope_is_refused_with_no_request(dd, writer, name):
    result = _run(name, MINIMAL[name], _ctx(UNCONFIGURED))

    assert result["refused"] is True
    assert "not configured for this organization" in result["error"]
    assert dd.requests == []


@pytest.mark.parametrize("name", [SPANS, MONITORS])
def test_an_escape_attempt_is_refused_with_no_request(dd, writer, name):
    result = _run(name, {"query": ") OR (*"})

    assert result["refused"] is True
    assert dd.requests == []


@pytest.mark.parametrize("name", TOOLS)
def test_a_refusal_is_judged_before_the_client_is_built(scopes, writer, monkeypatch, name):
    from robothor.engine.tools.handlers import datadog_more

    built = MagicMock()
    monkeypatch.setattr(datadog_more, "_client_factory", built)
    bad = {
        SPANS: {"query": ") OR (*"},
        TRACE: {"trace_id": "x OR *"},
        MONITORS: {"query": ") OR (*"},
        METRICS: {"metric": "m{*}"},
    }[name]

    result = _run(name, bad)

    assert result["refused"] is True
    built.assert_not_called()


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("status,kind", [(401, "auth"), (429, "rate_limit"), (400, "bad_query")])
def test_datadog_errors_surface_as_a_kind_only(dd, writer, name, status, kind):
    dd.status = status

    result = _run(name, MINIMAL[name])

    assert result["kind"] == kind
    text = json.dumps(result)
    for leak in (API_KEY, APP_KEY, "datadoghq", "https://", "vendor detail"):
        assert leak not in text


def _audit_rows(writer: MagicMock, event: str) -> list[dict[str, Any]]:
    return [c.kwargs for c in writer.submit.call_args_list if c.kwargs.get("event_type") == event]


def test_a_span_search_is_audited_with_query_window_and_count_never_span_text(dd, writer):
    dd.make_span = lambda i: _span(i, "SECRET-RESOURCE carol@example.com")

    _run(SPANS, {"query": "@usr.email:dave@example.com", "limit": 3})

    (row,) = _audit_rows(writer, "datadog.spans_read")
    assert (row["action"], row["actor"], row["tenant_id"]) == (SPANS, "ops-agent", CHILD_A)
    assert row["details"]["rows"] == 3
    assert row["details"]["query"] == "@usr.email:<email>"
    assert (row["details"]["start"], row["details"]["end"]) == (HOUR_AGO, NOW_ISO)
    text = json.dumps(row, default=str)
    assert "SECRET-RESOURCE" not in text and "dave@example.com" not in text


def test_a_trace_read_is_audited_with_the_id_and_span_count(dd, writer):
    dd.spans = [_span(0, "SECRET-RESOURCE"), _span(1)]

    _run(TRACE, {"trace_id": TRACE_ID})

    (row,) = _audit_rows(writer, "datadog.trace_read")
    assert row["details"]["query"] == TRACE_ID
    assert row["details"]["rows"] == 2
    assert row["status"] == "ok"
    assert "SECRET-RESOURCE" not in json.dumps(row, default=str)


def test_a_monitor_list_is_audited_with_the_query_and_count_never_names(dd, writer):
    dd.monitors = [_monitor(0, "SECRET-MONITOR-NAME")]

    _run(MONITORS, {"query": "team:checkout", "only_firing": True})

    (row,) = _audit_rows(writer, "datadog.monitors_read")
    assert row["details"]["rows"] == 1
    assert "team:checkout" in row["details"]["query"]
    assert row["details"]["only_firing"] is True
    assert "SECRET-MONITOR-NAME" not in json.dumps(row, default=str)


def test_a_metric_query_is_audited_with_the_metric_window_and_counts(dd, writer):
    _run(METRICS, {"metric": "trace.http.request.hits", "start": "now-4h"})

    (row,) = _audit_rows(writer, "datadog.metrics_read")
    details = row["details"]
    assert details["query"] == "trace.http.request.hits"
    assert (details["start"], details["end"]) == ("2026-11-25T08:00:00.000Z", NOW_ISO)
    assert (details["series"], details["points"]) == (1, 2)


@pytest.mark.parametrize("name", TOOLS)
def test_a_refused_read_is_audited_as_denied(dd, writer, name):
    _run(name, MINIMAL[name], _ctx(UNCONFIGURED))

    event = {
        SPANS: "datadog.spans_read",
        TRACE: "datadog.trace_read",
        MONITORS: "datadog.monitors_read",
        METRICS: "datadog.metrics_read",
    }[name]
    (row,) = _audit_rows(writer, event)
    assert row["status"] == "denied"


# ── the parent's review rules, held by every new tool ──────────────────────


def test_no_span_cursor_when_the_second_page_is_not_the_spans_returned(dd, writer):
    """Spans that arrive between the two calls shift the page; the cursor from
    the second call would skip or repeat spans, so none is handed out."""
    calls = {"n": 0}

    def shifting(i: int) -> dict[str, Any]:
        if i == 0:
            calls["n"] += 1
        offset = 0 if calls["n"] == 1 else 1000
        return _span(i + offset, "x" * 3000)

    dd.make_span = shifting

    result = _run(SPANS, {"limit": 300})

    assert result["truncated"] is True
    assert len(dd.requests) == 2
    assert result["next_cursor"] is None
    assert "narrow the query or window" in result["note"]


@pytest.mark.parametrize(
    "name,tenant",
    [(SPANS, NO_SPANS), (TRACE, NO_SPANS), (MONITORS, CHILD_B), (METRICS, CHILD_B)],
)
def test_a_scope_part_with_no_access_is_refused_as_not_configured(dd, writer, name, tenant):
    """The tenant's scope is the problem, not the input: no rewrite hint."""
    result = _run(name, MINIMAL[name], _ctx(tenant))

    assert result["refused"] is True
    assert result["gate"] == "runtime"
    assert "not configured for this organization" in result["error"]
    assert dd.requests == []


def test_phone_numbers_in_tags_are_masked_whatever_their_shape(dd, writer):
    dd.monitors = [_monitor(0, tags=["owner_phone:4155550199", "team:checkout"])]
    dd.series[0]["tag_set"] = ["mobile:+14155550123", "host:web-1"]

    monitors = _run(MONITORS, {})
    metrics = _run(METRICS, {"metric": "m"})

    assert monitors["monitors"][0]["tags"] == ["owner_phone:<phone>", "team:checkout"]
    assert metrics["series"][0]["tags"] == ["mobile:<phone>", "host:web-1"]
    assert "4155550" not in json.dumps([monitors, metrics])


def test_a_benchmark_run_is_never_offered_them():
    """They read real spans, monitors and metrics; a graded run touches nothing real."""
    from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

    assert set(TOOLS).isdisjoint(benchmark_readonly_tools())


# ── registration, read-only, opt-in ────────────────────────────────────────


def test_the_tools_are_registered_and_dispatchable():
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    handlers = builtin_handlers()
    schemas = get_engine_schemas()
    for name in TOOLS:
        assert name in handlers
        assert schemas[name]["function"]["name"] == name


def test_the_tools_are_read_only_opt_in_and_in_the_datadog_family():
    from robothor.engine.tools import DATADOG_TOOLS
    from robothor.engine.tools.constants import OPT_IN_TOOLS
    from robothor.engine.tools.read_only import declared_read_only_tools

    assert set(TOOLS) <= DATADOG_TOOLS
    assert DATADOG_TOOLS <= OPT_IN_TOOLS
    assert set(TOOLS) <= declared_read_only_tools()


@pytest.fixture(scope="module")
def registry():
    from robothor.engine.tools.registry import ToolRegistry

    return ToolRegistry()


def test_an_agent_with_no_grant_is_not_offered_them(registry):
    from robothor.engine.models import AgentConfig

    names = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))

    assert not names & set(TOOLS)


def test_tools_opt_in_offers_them(registry):
    from robothor.engine.models import AgentConfig

    config = AgentConfig(id="probe", name="Probe", tools_opt_in=list(TOOLS))

    assert set(TOOLS) <= set(registry.get_tool_names(config))


def test_tool_search_finds_them_by_the_words_an_agent_uses():
    from robothor.engine.tools.keywords import TOOL_HINTS

    expected = {
        SPANS: {"datadog", "spans", "latency", "slow"},
        TRACE: {"datadog", "trace", "spans"},
        MONITORS: {"datadog", "monitors", "alerts", "firing"},
        METRICS: {"datadog", "metrics", "timeseries"},
    }
    for name, words in expected.items():
        assert words <= set(TOOL_HINTS[name].keywords)


def test_the_schemas_advertise_the_tenant_argument(registry):
    for name in TOOLS:
        props = registry._schemas[name]["function"]["parameters"]["properties"]
        assert "tenant" in props


# ── cross-tenant: a parent may read a child; a child never a sibling ───────


def _session() -> SimpleNamespace:
    run = SimpleNamespace(
        parent_run_id=None, nesting_depth=0, trigger_type=TriggerType.WEBCHAT, is_benchmark=False
    )
    return SimpleNamespace(run=run)


@pytest.fixture
def dispatch_env(dd):
    from robothor.engine.tools.handlers import datadog_more

    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    with (
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=datadog_more.HANDLERS),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch("robothor.engine.session_registry.lookup", side_effect=lambda run_id: _session()),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
        patch("robothor.engine.event_writer.get_event_writer", return_value=MagicMock()),
    ):
        yield SimpleNamespace(dd=dd, audit=audit)


def _dispatch(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _execute_tool

    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": "alice",
        "user_role": "owner",
        "accessible_tenant_ids": (PARENT, CHILD_A, CHILD_B),
    }
    base.update(kw)
    return asyncio.run(_execute_tool(name, args, **base))


def test_the_tools_may_cross_tenants_as_reads():
    from robothor.engine.tools.constants import CROSS_TENANT_READS

    assert set(TOOLS) <= CROSS_TENANT_READS


#: What the child's scope puts in the one request each tool sends.
_CHILD_SCOPED = {
    SPANS: lambda dd: _attrs(dd)["filter"]["query"] == "(service:shop-api) AND (status:error)",
    TRACE: lambda dd: (
        _attrs(dd)["filter"]["query"] == f"(service:shop-api) AND (trace_id:{TRACE_ID})"
    ),
    MONITORS: lambda dd: dd.params()["query"] == '(tag:"service:shop-api") AND (status:alert)',
    METRICS: lambda dd: dd.params()["query"] == "avg:trace.http.request.hits{service:shop-api}",
}


@pytest.mark.parametrize("name", TOOLS)
def test_a_parent_naming_a_child_reads_with_the_childs_scope(dispatch_env, name):
    if name == TRACE:
        dispatch_env.dd.spans = [_span(0)]

    result = _dispatch(name, {"tenant": CHILD_A, **MINIMAL[name]})

    assert "error" not in result
    assert result["scope"] == CHILD_A
    assert _CHILD_SCOPED[name](dispatch_env.dd)
    dispatch_env.audit.assert_called_once()


@pytest.mark.parametrize("name", TOOLS)
def test_a_child_naming_a_sibling_is_refused_as_unreachable(dispatch_env, name):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    result = _dispatch(
        name,
        {"tenant": CHILD_B, **MINIMAL[name]},
        tenant_id=CHILD_A,
        user_role="member",
        accessible_tenant_ids=(CHILD_A,),
    )

    assert result["refused"] is True
    assert result["error"] == UNREACHABLE
    assert dispatch_env.dd.requests == []


# ── off the loop ───────────────────────────────────────────────────────────


async def test_the_client_is_built_off_the_event_loop(scopes, monkeypatch):
    """Building the client reads the vault, which blocks."""
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor
    from robothor.engine.tools.handlers import datadog_more
    from robothor.integrations.datadog.client import AsyncDatadogClient

    await warm_executor()
    fake = FakeDatadog()
    client = AsyncDatadogClient(API_KEY, APP_KEY, transport=httpx.MockTransport(fake.handler))
    probe = SlowProbe(asyncio.get_running_loop(), result=client)
    monkeypatch.setattr(datadog_more, "_client_factory", probe)

    with patch("robothor.engine.event_writer.get_event_writer", return_value=MagicMock()):
        await datadog_more.HANDLERS[SPANS]({"limit": 1}, _ctx())
    await probe.settle()

    probe.assert_loop_free()
