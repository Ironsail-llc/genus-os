"""Pure request builders and response parsers for the Datadog API: no I/O."""

from __future__ import annotations

import pytest

from robothor.integrations.datadog.requests import (
    Bucket,
    LogPage,
    LogRow,
    api_base,
    logs_aggregate,
    logs_search,
    parse_buckets,
    parse_log_page,
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
