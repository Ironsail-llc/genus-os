"""Pure request builders and response parsers for the Datadog API: no I/O."""

from __future__ import annotations

import pytest

from robothor.integrations.datadog.requests import (
    Bucket,
    LogPage,
    LogRow,
    MetricSeries,
    MonitorPage,
    MonitorRow,
    SpanPage,
    SpanRow,
    api_base,
    logs_aggregate,
    logs_search,
    metrics_query,
    monitors_search,
    parse_buckets,
    parse_log_page,
    parse_metric_series,
    parse_monitor_page,
    parse_span_buckets,
    parse_span_page,
    resolve_epoch,
    spans_aggregate,
    spans_search,
    validate_request,
)

# ── the host a key may be sent to ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("site", "base"),
    [
        ("datadoghq.com", "https://api.datadoghq.com"),
        ("us3.datadoghq.com", "https://api.us3.datadoghq.com"),
        ("us5.datadoghq.com", "https://api.us5.datadoghq.com"),
        ("datadoghq.eu", "https://api.datadoghq.eu"),
        ("ap1.datadoghq.com", "https://api.ap1.datadoghq.com"),
        ("ap2.datadoghq.com", "https://api.ap2.datadoghq.com"),
        ("ddog-gov.com", "https://api.ddog-gov.com"),
        ("  DatadogHQ.com ", "https://api.datadoghq.com"),
        ("app.datadoghq.com", "https://api.datadoghq.com"),
        ("https://app.datadoghq.eu/", "https://api.datadoghq.eu"),
    ],
)
def test_a_known_site_builds_its_api_host(site, base) -> None:
    assert api_base(site) == base


@pytest.mark.parametrize(
    "site",
    [
        "",
        "example.com",
        "datadoghq.com.example.com",
        "evil-datadoghq.com",
        "us9.datadoghq.com",
        "datadoghq.com/path",
        "datadoghq.com:8443",
        "@".join(("user", "datadoghq.com")),  # a userinfo prefix, built so it is not an address
        "http://datadoghq.com",
        "localhost",
    ],
)
def test_any_other_site_is_refused(site) -> None:
    assert api_base(site) is None


# ── logs search ──────────────────────────────────────────────────────────────


def test_logs_search_body() -> None:
    req = logs_search("(a) AND (b)", start="now-1h", end="now", limit=20, cursor=None)
    assert req.method == "POST"
    assert req.path == "/api/v2/logs/events/search"
    assert req.body == {
        "filter": {"query": "(a) AND (b)", "from": "now-1h", "to": "now"},
        "sort": "-timestamp",
        "page": {"limit": 20},
    }


def test_logs_search_cursor_and_oldest_first() -> None:
    req = logs_search(
        "q", start="2026-11-01T00:00:00Z", end="now", limit=5, cursor="abc", newest_first=False
    )
    assert req.body is not None
    assert req.body["sort"] == "timestamp"
    assert req.body["page"] == {"limit": 5, "cursor": "abc"}


def test_logs_search_limit_fits_the_api_page_bounds() -> None:
    high = logs_search("q", start="now-1h", end="now", limit=10**6).body
    low = logs_search("q", start="now-1h", end="now", limit=0).body
    assert high is not None and high["page"]["limit"] == 1000
    assert low is not None and low["page"]["limit"] == 1


def test_parse_log_page() -> None:
    body = {
        "data": [
            {
                "id": "AQAAAX",
                "type": "log",
                "attributes": {
                    "timestamp": "2026-11-25T10:00:00.000Z",
                    "service": "shop-api",
                    "status": "error",
                    "host": "web-1",
                    "message": "boom",
                    "tags": ["env:prod", "service:shop-api"],
                    "attributes": {"http": {"status_code": 500}},
                },
            }
        ],
        "meta": {"page": {"after": "next-cursor"}},
    }
    assert parse_log_page(body) == LogPage(
        rows=(
            LogRow(
                id="AQAAAX",
                timestamp="2026-11-25T10:00:00.000Z",
                service="shop-api",
                status="error",
                host="web-1",
                message="boom",
                tags=("env:prod", "service:shop-api"),
                attributes={"http": {"status_code": 500}},
            ),
        ),
        next_cursor="next-cursor",
    )


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "text",
        {},
        {"data": None},
        {"data": "x"},
        {"data": [None, 3, "x"]},
        {"data": [], "meta": {"page": None}},
        {"data": [], "meta": "x"},
    ],
)
def test_parse_log_page_tolerates_any_shape(body) -> None:
    assert parse_log_page(body) == LogPage(rows=(), next_cursor=None)


def test_a_row_with_missing_fields_is_empty_values() -> None:
    page = parse_log_page({"data": [{"attributes": {"tags": "notalist", "attributes": 7}}]})
    assert page.rows == (
        LogRow(
            id="",
            timestamp="",
            service="",
            status="",
            host="",
            message="",
            tags=(),
            attributes={},
        ),
    )


# ── logs aggregate ──────────────────────────────────────────────────────────


def test_logs_aggregate_body() -> None:
    req = logs_aggregate(
        "(a) AND (b)", start="now-1h", end="now", group_by=["service", "status"], limit=10
    )
    assert req.method == "POST"
    assert req.path == "/api/v2/logs/analytics/aggregate"
    sort = {"aggregation": "count", "order": "desc", "type": "measure"}
    assert req.body == {
        "filter": {"query": "(a) AND (b)", "from": "now-1h", "to": "now"},
        "compute": [{"aggregation": "count"}],
        "group_by": [
            {"facet": "service", "limit": 10, "sort": sort},
            {"facet": "status", "limit": 10, "sort": sort},
        ],
    }


def test_logs_aggregate_with_no_grouping_counts_everything() -> None:
    req = logs_aggregate("q", start="now-1h", end="now", group_by=[], limit=10)
    assert req.body is not None and "group_by" not in req.body


def test_parse_buckets() -> None:
    body = {
        "data": {
            "buckets": [
                {"by": {"service": "shop-api", "status": "error"}, "computes": {"c0": 12}},
                {"by": {"@http.status_code": 500}, "computes": {"c0": 3.0}},
            ]
        }
    }
    assert parse_buckets(body) == (
        Bucket(by={"service": "shop-api", "status": "error"}, count=12),
        Bucket(by={"@http.status_code": "500"}, count=3),
    )


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"data": None}, {"data": {"buckets": None}}, {"data": {"buckets": [None, 1]}}],
)
def test_parse_buckets_tolerates_any_shape(body) -> None:
    assert parse_buckets(body) == ()


def test_a_bucket_with_missing_or_odd_fields() -> None:
    body = {"data": {"buckets": [{"by": None, "computes": {"c0": "many"}}, {}]}}
    assert parse_buckets(body) == (Bucket(by={}, count=0), Bucket(by={}, count=0))


# ── validate ────────────────────────────────────────────────────────────────


def test_validate_request() -> None:
    req = validate_request()
    assert (req.method, req.path, req.body) == ("GET", "/api/v1/validate", None)


# ── spans search ────────────────────────────────────────────────────────────


def test_spans_search_body() -> None:
    req = spans_search("(a) AND (b)", start="now-1h", end="now", limit=20)
    assert req.method == "POST"
    assert req.path == "/api/v2/spans/events/search"
    assert req.body == {
        "data": {
            "type": "search_request",
            "attributes": {
                "filter": {"query": "(a) AND (b)", "from": "now-1h", "to": "now"},
                "sort": "-timestamp",
                "page": {"limit": 20},
            },
        }
    }


def test_spans_search_cursor_oldest_first_and_page_bounds() -> None:
    req = spans_search("q", start="now-1h", end="now", limit=10**6, cursor="c", newest_first=False)
    assert req.body is not None
    attrs = req.body["data"]["attributes"]
    assert attrs["sort"] == "timestamp"
    assert attrs["page"] == {"limit": 1000, "cursor": "c"}
    low = spans_search("q", start="now-1h", end="now", limit=0).body
    assert low is not None and low["data"]["attributes"]["page"]["limit"] == 1


SPAN_ITEM = {
    "id": "AAAA",
    "type": "spans",
    "attributes": {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "1111",
        "parent_id": "0",
        "service": "shop-api",
        "resource_name": "GET /checkout",
        "operation_name": "http.request",
        "start_timestamp": "2026-11-25T10:00:00.000Z",
        "end_timestamp": "2026-11-25T10:00:00.250Z",
        "status": "error",
        "env": "prod",
        "tags": ["env:prod", "service:shop-api"],
        "custom": {"duration": 250000000, "http": {"status_code": 500}},
    },
}


def test_parse_span_page() -> None:
    page = parse_span_page({"data": [SPAN_ITEM], "meta": {"page": {"after": "next"}}})
    assert page == SpanPage(
        rows=(
            SpanRow(
                trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
                span_id="1111",
                parent_id="0",
                service="shop-api",
                resource_name="GET /checkout",
                operation_name="http.request",
                start_timestamp="2026-11-25T10:00:00.000Z",
                end_timestamp="2026-11-25T10:00:00.250Z",
                duration_ns=250000000,
                status="error",
                env="prod",
                tags=("env:prod", "service:shop-api"),
                attributes={"duration": 250000000, "http": {"status_code": 500}},
            ),
        ),
        next_cursor="next",
    )


def test_a_span_duration_may_sit_under_attributes() -> None:
    item = {"attributes": {"attributes": {"duration": 1.5e6}}}
    assert parse_span_page({"data": [item]}).rows[0].duration_ns == 1500000


def test_a_span_status_and_operation_may_sit_under_custom() -> None:
    item = {"attributes": {"custom": {"status": "ok", "operation_name": "db.query"}}}
    row = parse_span_page({"data": [item]}).rows[0]
    assert (row.status, row.operation_name) == ("ok", "db.query")


@pytest.mark.parametrize(
    "body",
    [None, [], "x", {}, {"data": None}, {"data": [None, 2]}, {"data": [], "meta": {"page": 3}}],
)
def test_parse_span_page_tolerates_any_shape(body) -> None:
    assert parse_span_page(body) == SpanPage(rows=(), next_cursor=None)


def test_a_span_with_missing_or_odd_fields_is_empty_values() -> None:
    item = {"attributes": {"tags": "x", "custom": 3, "attributes": {"duration": "long"}}}
    assert parse_span_page({"data": [item]}).rows == (
        SpanRow(
            trace_id="",
            span_id="",
            parent_id="",
            service="",
            resource_name="",
            operation_name="",
            start_timestamp="",
            end_timestamp="",
            duration_ns=None,
            status="",
            env="",
            tags=(),
            attributes={},
        ),
    )


# ── spans aggregate ─────────────────────────────────────────────────────────


def test_spans_aggregate_body() -> None:
    req = spans_aggregate("(a) AND (b)", start="now-1h", end="now", group_by=["service"], limit=5)
    assert req.method == "POST"
    assert req.path == "/api/v2/spans/analytics/aggregate"
    sort = {"aggregation": "count", "order": "desc", "type": "measure"}
    assert req.body == {
        "data": {
            "type": "aggregate_request",
            "attributes": {
                "filter": {"query": "(a) AND (b)", "from": "now-1h", "to": "now"},
                "compute": [{"aggregation": "count"}],
                "group_by": [{"facet": "service", "limit": 5, "sort": sort}],
            },
        }
    }


def test_spans_aggregate_with_no_grouping() -> None:
    req = spans_aggregate("q", start="now-1h", end="now", group_by=[])
    assert req.body is not None and "group_by" not in req.body["data"]["attributes"]


def test_parse_span_buckets() -> None:
    body = {
        "data": [
            {"type": "bucket", "attributes": {"by": {"service": "shop-api"}, "compute": {"c0": 9}}},
            {"attributes": {"by": {"@http.status_code": 500}, "compute": {"c0": 2.0}}},
        ]
    }
    assert parse_span_buckets(body) == (
        Bucket(by={"service": "shop-api"}, count=9),
        Bucket(by={"@http.status_code": "500"}, count=2),
    )


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"data": None}, {"data": {"buckets": []}}, {"data": [None, 1]}],
)
def test_parse_span_buckets_tolerates_any_shape(body) -> None:
    assert parse_span_buckets(body) == ()


def test_a_span_bucket_with_missing_fields() -> None:
    body = {"data": [{}, {"attributes": {"by": 3, "compute": {"c0": "x"}}}]}
    assert parse_span_buckets(body) == (Bucket(by={}, count=0), Bucket(by={}, count=0))


# ── monitors ────────────────────────────────────────────────────────────────


def test_monitors_search_request() -> None:
    req = monitors_search("(team:shop) AND (status:alert)", page=2, limit=25)
    assert (req.method, req.path, req.body) == ("GET", "/api/v1/monitor/search", None)
    assert req.params == {"query": "(team:shop) AND (status:alert)", "page": "2", "per_page": "25"}


def test_monitors_search_fits_the_page_arguments() -> None:
    req = monitors_search("q", page=-3, limit=0)
    assert req.params is not None
    assert (req.params["page"], req.params["per_page"]) == ("0", "1")


def test_parse_monitor_page() -> None:
    body = {
        "monitors": [
            {
                "id": 123,
                "name": "High error rate on shop-api",
                "status": "Alert",
                "type": "query alert",
                "tags": ["team:shop", "service:shop-api"],
                "last_triggered_ts": 1795600800,
            }
        ],
        "metadata": {"total_count": 41, "page": 1, "page_count": 3, "per_page": 20},
    }
    assert parse_monitor_page(body) == MonitorPage(
        rows=(
            MonitorRow(
                id="123",
                name="High error rate on shop-api",
                status="Alert",
                type="query alert",
                tags=("team:shop", "service:shop-api"),
                last_triggered_ts=1795600800,
            ),
        ),
        total_count=41,
        page=1,
        page_count=3,
    )


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"monitors": None}, {"monitors": [None, 3]}, {"metadata": "x"}],
)
def test_parse_monitor_page_tolerates_any_shape(body) -> None:
    assert parse_monitor_page(body) == MonitorPage(rows=(), total_count=0, page=0, page_count=0)


def test_a_monitor_with_missing_fields_is_empty_values() -> None:
    page = parse_monitor_page({"monitors": [{"tags": "x", "last_triggered_ts": "soon"}]})
    assert page.rows == (
        MonitorRow(id="", name="", status="", type="", tags=(), last_triggered_ts=None),
    )


# ── metrics ─────────────────────────────────────────────────────────────────


def test_metrics_query_request() -> None:
    req = metrics_query("avg:m{*}", start=1795597200, end=1795600800)
    assert (req.method, req.path, req.body) == ("GET", "/api/v1/query", None)
    assert req.params == {"from": "1795597200", "to": "1795600800", "query": "avg:m{*}"}


def test_parse_metric_series_skips_null_points() -> None:
    body = {
        "status": "ok",
        "series": [
            {
                "metric": "trace.http.request.hits",
                "scope": "service:shop-api,env:prod",
                "tag_set": ["service:shop-api", "env:prod"],
                "pointlist": [[1795597200000.0, 3.0], [1795597260000.0, None], [1795597320000, 5]],
            }
        ],
    }
    assert parse_metric_series(body) == (
        MetricSeries(
            metric="trace.http.request.hits",
            scope="service:shop-api,env:prod",
            tags=("service:shop-api", "env:prod"),
            points=((1795597200000, 3.0), (1795597320000, 5.0)),
        ),
    )


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"series": None}, {"series": [None, "x"]}],
)
def test_parse_metric_series_tolerates_any_shape(body) -> None:
    assert parse_metric_series(body) == ()


def test_a_series_with_odd_points_keeps_only_the_good_ones() -> None:
    body = {"series": [{"pointlist": [None, [1], ["a", 2], [1000, "x"], [2000, True], [3000, 1]]}]}
    assert parse_metric_series(body) == (
        MetricSeries(metric="", scope="", tags=(), points=((3000, 1.0),)),
    )


# ── the metric time window ──────────────────────────────────────────────────

NOW = 1795600800.0  # 2026-11-25T10:00:00Z


@pytest.mark.parametrize(
    ("value", "epoch"),
    [
        ("now", 1795600800),
        (" now ", 1795600800),
        ("now-30s", 1795600770),
        ("now-15m", 1795599900),
        ("now-1h", 1795597200),
        ("now-2d", 1795428000),
        ("2026-11-25T10:00:00Z", 1795600800),
        ("2026-11-25T10:00:00+00:00", 1795600800),
        ("2026-11-25T11:00:00+01:00", 1795600800),
        ("2026-11-25T10:00:00.750Z", 1795600800),
        ("2026-11-25T10:00:00", 1795600800),
    ],
)
def test_resolve_epoch_accepts_now_relative_and_iso(value, epoch) -> None:
    assert resolve_epoch(value, now=NOW) == epoch


@pytest.mark.parametrize(
    "value",
    ["", "yesterday", "now-", "now-1", "now-1y", "now+1h", "now-1h-1m", "now - 1h", "-1h", "17640"],
)
def test_resolve_epoch_refuses_anything_else(value) -> None:
    with pytest.raises(ValueError) as caught:
        resolve_epoch(value, now=NOW)
    assert "now-" in str(caught.value)


# ── trace and span ids ──────────────────────────────────────────────────────

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"


def _one(attributes: dict) -> LogRow:
    page = parse_log_page({"data": [{"id": "1", "attributes": attributes}]})
    return page.rows[0]


def test_the_reserved_top_level_trace_and_span_ids_are_read() -> None:
    row = _one({"message": "boom", "trace_id": TRACE, "span_id": SPAN})
    assert (row.trace_id, row.span_id) == (TRACE, SPAN)


@pytest.mark.parametrize(
    "nested",
    [
        {"trace_id": TRACE, "span_id": SPAN},
        {"dd": {"trace_id": TRACE, "span_id": SPAN}},
        {"otel": {"trace_id": TRACE, "span_id": SPAN}},
    ],
)
def test_the_nested_attribute_forms_are_fallbacks(nested) -> None:
    row = _one({"message": "boom", "attributes": nested})
    assert (row.trace_id, row.span_id) == (TRACE, SPAN)


def test_the_fallbacks_apply_in_order() -> None:
    row = _one(
        {
            "trace_id": "",
            "attributes": {
                "trace_id": "first",
                "dd": {"trace_id": "second"},
                "otel": {"trace_id": "third", "span_id": "otel-span"},
            },
        }
    )
    assert row.trace_id == "first"
    assert row.span_id == "otel-span"
    row = _one({"attributes": {"dd": {"trace_id": "second"}, "otel": {"trace_id": "third"}}})
    assert row.trace_id == "second"
    top = _one({"trace_id": TRACE, "attributes": {"trace_id": "nested"}})
    assert top.trace_id == TRACE


def test_a_numeric_trace_id_is_kept_as_text() -> None:
    assert _one({"attributes": {"dd": {"trace_id": 1234567890123}}}).trace_id == "1234567890123"


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {"trace_id": None, "span_id": {"x": 1}},
        {"attributes": {"dd": "not-a-map", "otel": ["x"]}},
        {"attributes": {"trace_id": True}},
    ],
)
def test_a_missing_or_odd_trace_id_is_empty(attributes) -> None:
    row = _one(attributes)
    assert (row.trace_id, row.span_id) == ("", "")


def test_the_new_fields_default_so_old_constructors_still_work() -> None:
    row = LogRow("1", "", "", "", "", "", ())
    assert (row.trace_id, row.span_id, row.attributes) == ("", "", {})
