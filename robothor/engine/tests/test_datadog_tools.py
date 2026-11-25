"""``datadog_logs_search`` and ``datadog_logs_aggregate``: a tenant reads its own logs.

Several tenants share one Datadog account. What keeps them apart is the scope
filter :mod:`robothor.integrations.datadog.scope` composes into every query,
and these tools take that scope from the run's tenant (``ctx.tenant_id``) and
nothing else. No test reaches the network: the handler's client is built by
``datadog_logs._client_factory``, replaced here with one whose transport is an
in-process fake answering in Datadog's own response shapes.
"""

from __future__ import annotations

import asyncio
import json
import threading
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
SEARCH, AGGREGATE = "datadog_logs_search", "datadog_logs_aggregate"
API_KEY, APP_KEY = "dd-api-key-0123456789", "dd-app-key-9876543210"

# Phone-shaped fixtures, assembled at runtime so no source line is shaped like
# a phone number (the instance-leak gate scans this file).
_AREA, _EXCHANGE = "415", "555"
PHONE_SPACED = " ".join(("+1", _AREA, _EXCHANGE, "0100"))
PHONE_DASHED = "-".join((_AREA, _EXCHANGE, "0199"))
PHONE_DIGITS = "".join(("1", _AREA, _EXCHANGE, "0199"))
PHONE_NUMBER = int(PHONE_DIGITS)

NO_LOGS = "acme-child-c"
SCOPES = {
    PARENT: {"logs": "*"},
    CHILD_A: {"logs": "service:shop-api"},
    CHILD_B: {"logs": "service:clinic-app"},
    # An entry that grants no log access: the scope itself is refused.
    NO_LOGS: {"logs": 'tag:"genus_scope:no_access"'},
}

#: The fixed "now" every test runs at, and the default window it resolves to.
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
HOUR_AGO, NOW_ISO = "2026-11-25T11:00:00.000Z", "2026-11-25T12:00:00.000Z"


# ── a fake Datadog ─────────────────────────────────────────────────────────


def _log(i: int, message: str = "", **attrs: Any) -> dict[str, Any]:
    return {
        "id": f"log-{i}",
        "type": "log",
        "attributes": {
            "timestamp": f"2026-11-25T10:{i % 60:02d}:00.000Z",
            "service": "shop-api",
            "status": "error",
            "host": "web-1",
            "message": message or f"request {i} failed",
            "tags": ["env:prod", "version:1.4.2", "team:checkout"],
            "attributes": {"http": {"status_code": 500}, **attrs},
        },
    }


class FakeDatadog:
    """Answers the two log endpoints and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        # A log row per page slot; a page holds `page.limit` of them.
        self.make_row = lambda i: _log(i)
        self.buckets: list[dict[str, Any]] = [
            {"by": {"service": "shop-api", "status": "error"}, "computes": {"c0": 12}},
            {"by": {"service": "shop-api", "status": "warn"}, "computes": {"c0": 3}},
        ]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": ["vendor detail with secrets"]})
        body = json.loads(request.content)
        if request.url.path == "/api/v2/logs/events/search":
            limit = body["page"]["limit"]
            return httpx.Response(
                200,
                json={
                    "data": [self.make_row(i) for i in range(limit)],
                    # The cursor names how many rows the page held, so a test
                    # can tell which page a returned cursor continues after.
                    "meta": {"page": {"after": f"after-{limit}"}},
                },
            )
        if request.url.path == "/api/v2/logs/analytics/aggregate":
            return httpx.Response(200, json={"data": {"buckets": self.buckets}})
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
    from robothor.engine.tools.handlers import datadog_logs

    monkeypatch.setattr(datadog_logs, "_now", lambda: NOW, raising=False)


@pytest.fixture
def dd(monkeypatch, scopes):
    from robothor.engine.tools.handlers import datadog_logs
    from robothor.integrations.datadog.client import AsyncDatadogClient

    fake = FakeDatadog()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(
        datadog_logs,
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
    from robothor.engine.tools.handlers import datadog_logs

    return asyncio.run(datadog_logs.HANDLERS[name](args, ctx or _ctx()))


# ── the scope is the tenant's, composed into every query ──────────────────


def test_search_sends_the_tenants_scope_with_the_callers_query(dd, writer):
    result = _run(SEARCH, {"query": "status:error"})

    assert "error" not in result
    (body,) = dd.bodies()
    assert body["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert result["scope"] == CHILD_A


def test_search_defaults_to_the_last_hour_newest_first(dd, writer):
    result = _run(SEARCH, {})

    (body,) = dd.bodies()
    assert body["filter"]["query"] == "service:shop-api"
    # The relative default is resolved once, so the window the model is handed
    # back (and pages with) is the one that was searched.
    assert (body["filter"]["from"], body["filter"]["to"]) == (HOUR_AGO, NOW_ISO)
    assert body["sort"] == "-timestamp"
    assert 1 <= body["page"]["limit"] <= 100
    assert result["window"] == {"start": HOUR_AGO, "end": NOW_ISO}


@pytest.mark.parametrize(
    "start,end,sent",
    [
        ("now-15m", "now", ("2026-11-25T11:45:00.000Z", NOW_ISO)),
        ("now-2d", "now-1d", ("2026-11-23T12:00:00.000Z", "2026-11-24T12:00:00.000Z")),
        ("now-1w", "now-30s", ("2026-11-18T12:00:00.000Z", "2026-11-25T11:59:30.000Z")),
        # Absolute times are sent exactly as given.
        ("2026-11-25T09:00:00Z", "2026-11-25T10:00:00Z", None),
        # So is a unit the handler does not know, case included: `M` is not `m`.
        ("now-1M", "2026-11-25T10:00:00Z", None),
    ],
)
def test_a_relative_window_is_resolved_to_absolute_times_once(dd, writer, start, end, sent):
    result = _run(SEARCH, {"start": start, "end": end})

    expected = sent or (start, end)
    (body,) = dd.bodies()
    assert (body["filter"]["from"], body["filter"]["to"]) == expected
    assert result["window"] == {"start": expected[0], "end": expected[1]}


def test_aggregate_resolves_its_window_the_same_way(dd, writer):
    result = _run(AGGREGATE, {"start": "now-15m", "group_by": ["service"]})

    (body,) = dd.bodies()
    assert (body["filter"]["from"], body["filter"]["to"]) == ("2026-11-25T11:45:00.000Z", NOW_ISO)
    assert result["window"] == {"start": "2026-11-25T11:45:00.000Z", "end": NOW_ISO}


def test_search_passes_window_limit_cursor_and_order(dd, writer):
    _run(
        SEARCH,
        {
            "query": "@http.status_code:500",
            "start": "2026-11-25T09:00:00Z",
            "end": "2026-11-25T10:00:00Z",
            "limit": 7,
            "cursor": "abc",
            "newest_first": False,
        },
    )

    (body,) = dd.bodies()
    assert body["filter"]["from"] == "2026-11-25T09:00:00Z"
    assert body["filter"]["to"] == "2026-11-25T10:00:00Z"
    assert body["page"] == {"limit": 7, "cursor": "abc"}
    assert body["sort"] == "timestamp"


def test_search_rows_carry_the_fields_an_agent_reads(dd, writer):
    result = _run(SEARCH, {"limit": 2})

    assert result["next_cursor"] == "after-2"
    assert result["rows_returned"] == 2
    row = result["rows"][0]
    assert row["timestamp"] == "2026-11-25T10:00:00.000Z"
    assert (row["service"], row["status"], row["host"]) == ("shop-api", "error", "web-1")
    assert row["message"] == "request 0 failed"
    assert row["tags"] == {"env": "prod", "version": "1.4.2"}
    assert row["attributes"] == {"http": {"status_code": 500}}


def test_the_scope_filter_text_is_never_returned(dd, writer):
    result = _run(SEARCH, {"query": "status:error"})

    assert "service:shop-api" not in json.dumps({k: v for k, v in result.items() if k != "rows"})


def test_aggregate_counts_grouped_by_facets_inside_the_scope(dd, writer):
    result = _run(AGGREGATE, {"query": "status:error", "group_by": ["service", "status"]})

    (body,) = dd.bodies()
    assert body["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert [g["facet"] for g in body["group_by"]] == ["service", "status"]
    assert (body["filter"]["from"], body["filter"]["to"]) == (HOUR_AGO, NOW_ISO)
    assert result["scope"] == CHILD_A
    assert result["buckets"] == [
        {"by": {"service": "shop-api", "status": "error"}, "count": 12},
        {"by": {"service": "shop-api", "status": "warn"}, "count": 3},
    ]


def test_a_parent_with_everything_sends_the_callers_query_alone(dd, writer):
    _run(SEARCH, {"query": "status:error"}, _ctx(PARENT))

    (body,) = dd.bodies()
    assert body["filter"]["query"] == "status:error"


# ── refusals: nothing is sent ──────────────────────────────────────────────


@pytest.mark.parametrize("name", [SEARCH, AGGREGATE])
def test_a_tenant_with_no_scope_is_refused_with_no_request(dd, writer, name):
    result = _run(name, {"query": "status:error", "group_by": ["service"]}, _ctx(UNCONFIGURED))

    assert result["refused"] is True
    assert "not configured for this organization" in result["error"]
    assert dd.requests == []


@pytest.mark.parametrize("name", [SEARCH, AGGREGATE])
def test_a_scope_with_no_log_access_is_refused_as_not_configured(dd, writer, name):
    """The tenant's scope is the problem, not the query: no rewrite hint."""
    result = _run(name, {"query": "status:error", "group_by": ["service"]}, _ctx(NO_LOGS))

    assert result["refused"] is True
    assert result["gate"] == "runtime"
    assert "not configured for this organization" in result["error"]
    assert "bracket" not in result["unlock"]
    assert dd.requests == []


@pytest.mark.parametrize("name", [SEARCH, AGGREGATE])
def test_an_escape_attempt_is_refused_with_no_request(dd, writer, name):
    result = _run(name, {"query": ") OR (*", "group_by": ["service"]})

    assert result["refused"] is True
    assert "brackets or parentheses" in result["error"]
    assert dd.requests == []


def test_an_escape_is_refused_even_when_no_key_is_configured(scopes, writer, monkeypatch):
    """The query is judged before the client is built, so the refusal is the
    one the agent can act on, not a missing-key error."""
    from robothor.engine.tools.handlers import datadog_logs

    built = MagicMock()
    monkeypatch.setattr(datadog_logs, "_client_factory", built)

    result = _run(SEARCH, {"query": ") OR (*"})

    assert result["refused"] is True
    built.assert_not_called()


@pytest.mark.parametrize("status,kind", [(401, "auth"), (429, "rate_limit"), (400, "bad_query")])
def test_datadog_errors_surface_as_a_kind_without_url_key_or_body(dd, writer, status, kind):
    dd.status = status

    result = _run(SEARCH, {"query": "status:error"})

    assert result["kind"] == kind
    assert "error" in result
    text = json.dumps(result)
    for leak in (API_KEY, APP_KEY, "datadoghq", "https://", "vendor detail"):
        assert leak not in text


def test_a_missing_key_is_a_not_configured_error(scopes, writer, monkeypatch):
    from robothor.engine.tools.handlers import datadog_logs
    from robothor.integrations.datadog.client import DatadogError

    def _no_key() -> Any:
        raise DatadogError("not_configured", detail="the Datadog API or application key is empty")

    monkeypatch.setattr(datadog_logs, "_client_factory", _no_key)

    result = _run(AGGREGATE, {"group_by": ["service"]})

    assert result["kind"] == "not_configured"


# ── privacy and fitting ────────────────────────────────────────────────────


def test_email_and_phone_in_messages_and_attributes_are_masked(dd, writer):
    dd.make_row = lambda i: _log(
        i,
        f"login failed for alice@example.com, call {PHONE_SPACED}",
        usr={"email": "bob@example.com", "phone": PHONE_DASHED},
    )

    result = _run(SEARCH, {"limit": 1})

    text = json.dumps(result)
    assert "alice@example.com" not in text
    assert "bob@example.com" not in text
    assert " ".join((_EXCHANGE, "0100")) not in text
    assert PHONE_DASHED not in text
    row = result["rows"][0]
    assert "<email>" in row["message"] and "<phone>" in row["message"]
    assert row["attributes"]["usr"] == {"email": "<email>", "phone": "<phone>"}


def test_personal_data_stored_as_a_number_is_masked(dd, writer):
    dd.make_row = lambda i: _log(i, usr={"phone": PHONE_NUMBER, "id": 42}, retries=3)

    result = _run(SEARCH, {"limit": 1})

    attrs = result["rows"][0]["attributes"]
    assert PHONE_DIGITS not in json.dumps(result)
    assert attrs["usr"]["phone"] == "<phone>"
    # A number that is not personal data keeps its type.
    assert attrs["usr"]["id"] == 42
    assert attrs["retries"] == 3
    assert attrs["http"] == {"status_code": 500}


#: Bare credential values, as Datadog leaves them after parsing
#: ``password=... api_key=...`` out of a line: no shape a pattern can match.
CREDENTIALS = ("hunter2hunter2", "abc123abc123", "zzz999zzz999")


def test_values_under_credential_named_keys_never_reach_the_result(dd, writer):
    def row(i: int) -> dict[str, Any]:
        raw = _log(
            i,
            password="hunter2hunter2",
            db={"api_key": "abc123abc123"},
            items=[{"token": "zzz999zzz999"}],
        )
        raw["attributes"]["tags"].append("api_key:abc123abc123")
        return raw

    dd.make_row = row

    result = _run(SEARCH, {"limit": 1})

    text = json.dumps(result)
    for secret in CREDENTIALS:
        assert secret not in text
    attrs = result["rows"][0]["attributes"]
    assert attrs["password"] == "<redacted>"
    assert attrs["db"] == {"api_key": "<redacted>"}
    assert attrs["items"] == [{"token": "<redacted>"}]
    # Ordinary attributes are untouched.
    assert attrs["http"] == {"status_code": 500}


def test_aggregate_grouped_by_a_credential_facet_withholds_its_values(dd, writer):
    dd.buckets = [{"by": {"@usr.password": "hunter2hunter2"}, "computes": {"c0": 2}}]

    result = _run(AGGREGATE, {"group_by": ["@usr.password"]})

    assert "hunter2hunter2" not in json.dumps(result)
    assert result["buckets"] == [{"by": {"@usr.password": "<redacted>"}, "count": 2}]


def test_aggregate_bucket_values_are_masked(dd, writer):
    dd.buckets = [{"by": {"@usr.email": "alice@example.com"}, "computes": {"c0": 4}}]

    result = _run(AGGREGATE, {"group_by": ["@usr.email"]})

    assert result["buckets"] == [{"by": {"@usr.email": "<email>"}, "count": 4}]


def test_an_oversized_page_is_fitted_with_a_note_and_a_cursor_that_continues(dd, writer):
    from robothor.engine.tools.handlers import datadog_logs

    dd.make_row = lambda i: _log(i, "x" * 5000, payload={"blob": "y" * 3000})

    result = _run(SEARCH, {"limit": 200})

    assert result["truncated"] is True
    kept = len(result["rows"])
    assert 1 <= kept < 200
    assert result["rows_returned"] == kept
    assert "note" in result and str(kept) in result["note"]
    # The cursor continues right after the last row returned, not after the
    # 200th: the rows that did not fit are on the next page.
    assert result["next_cursor"] == f"after-{kept}"
    assert len(json.dumps(result)) <= datadog_logs.RESULT_BUDGET_CHARS


def _traced(i: int, message: str = "", **attrs: Any) -> dict[str, Any]:
    """A log carrying its trace and span ids in the event's reserved fields."""
    raw = _log(i, message, **attrs)
    raw["attributes"]["trace_id"] = f"trace-{i}"
    raw["attributes"]["span_id"] = f"span-{i}"
    return raw


def test_a_row_carries_its_trace_and_span_ids(dd, writer):
    dd.make_row = _traced

    result = _run(SEARCH, {"limit": 2})

    row = result["rows"][1]
    assert (row["trace_id"], row["span_id"]) == ("trace-1", "span-1")


def test_a_row_without_a_trace_has_no_trace_fields(dd, writer):
    result = _run(SEARCH, {"limit": 1})

    row = result["rows"][0]
    assert "trace_id" not in row and "span_id" not in row


def test_fitting_keeps_the_trace_ids_when_it_drops_attributes(dd, writer):
    dd.make_row = lambda i: _traced(i, "x" * 5000, payload={"blob": "y" * 3000})

    result = _run(SEARCH, {"limit": 200})

    assert result["truncated"] is True
    for row in result["rows"]:
        assert row.get("attributes_omitted") is True
        assert row["trace_id"].startswith("trace-")
        assert row["span_id"].startswith("span-")


def test_no_cursor_when_the_second_page_is_not_the_rows_returned(dd, writer):
    """Logs that arrive between the two calls shift the page; the cursor from
    the second call would skip or repeat rows, so none is handed out."""
    calls = {"n": 0}

    def shifting(i: int) -> dict[str, Any]:
        if i == 0:
            calls["n"] += 1
        offset = 0 if calls["n"] == 1 else 1000
        return _log(i + offset, "x" * 5000, payload={"blob": "y" * 3000})

    dd.make_row = shifting

    result = _run(SEARCH, {"limit": 200})

    assert result["truncated"] is True
    assert len(dd.requests) == 2
    assert result["next_cursor"] is None
    assert "narrow the query or window" in result["note"]


def test_a_page_that_fits_is_not_marked_truncated(dd, writer):
    result = _run(SEARCH, {"limit": 3})

    assert result["truncated"] is False
    assert "note" not in result
    assert len(dd.requests) == 1


# ── the audit row ──────────────────────────────────────────────────────────


def _audit_rows(writer: MagicMock) -> list[dict[str, Any]]:
    return [
        c.kwargs
        for c in writer.submit.call_args_list
        if c.kwargs.get("event_type") == "datadog.logs_read"
    ]


def test_a_search_is_audited_with_the_query_and_row_count_never_the_logs(dd, writer):
    dd.make_row = lambda i: _log(i, "SECRET-LOG-BODY for carol@example.com")

    _run(SEARCH, {"query": "status:error @usr.email:dave@example.com", "limit": 4})

    (row,) = _audit_rows(writer)
    details = row["details"]
    assert row["action"] == SEARCH
    assert row["actor"] == "ops-agent"
    assert row["tenant_id"] == CHILD_A
    assert details["rows"] == 4
    assert details["query"] == "status:error @usr.email:<email>"
    assert (details["start"], details["end"]) == (HOUR_AGO, NOW_ISO)
    text = json.dumps(row, default=str)
    assert "SECRET-LOG-BODY" not in text
    assert "carol@example.com" not in text
    assert "dave@example.com" not in text


def test_an_aggregate_is_audited_with_its_bucket_count(dd, writer):
    _run(AGGREGATE, {"query": "status:error", "group_by": ["service"]})

    (row,) = _audit_rows(writer)
    assert row["details"]["rows"] == 2
    assert row["details"]["group_by"] == ["service"]


def test_a_refused_query_is_audited_as_denied(dd, writer):
    _run(SEARCH, {"query": ") OR (*"})

    (row,) = _audit_rows(writer)
    assert row["status"] == "denied"
    assert row["details"]["rows"] == 0


# ── registration, read-only, opt-in ────────────────────────────────────────


def test_both_tools_are_registered_and_dispatchable():
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    handlers = builtin_handlers()
    schemas = get_engine_schemas()
    for name in (SEARCH, AGGREGATE):
        assert name in handlers
        assert schemas[name]["function"]["name"] == name


def test_both_tools_are_read_only():
    from robothor.engine.tools.read_only import declared_read_only_tools

    assert {SEARCH, AGGREGATE} <= declared_read_only_tools()


def test_both_tools_form_an_opt_in_family():
    from robothor.engine.tools import DATADOG_TOOLS
    from robothor.engine.tools.constants import OPT_IN_TOOLS

    assert frozenset({SEARCH, AGGREGATE}) == DATADOG_TOOLS
    assert DATADOG_TOOLS <= OPT_IN_TOOLS


@pytest.fixture(scope="module")
def registry():
    from robothor.engine.tools.registry import ToolRegistry

    return ToolRegistry()


def test_a_benchmark_run_is_never_offered_them():
    """They read real logs; a graded run touches nothing real."""
    from robothor.engine.tools import DATADOG_TOOLS
    from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

    assert DATADOG_TOOLS.isdisjoint(benchmark_readonly_tools())


def test_an_agent_with_no_grant_is_not_offered_them(registry):
    from robothor.engine.models import AgentConfig

    names = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))

    assert not names & {SEARCH, AGGREGATE}


def test_tools_opt_in_offers_them(registry):
    from robothor.engine.models import AgentConfig

    config = AgentConfig(id="probe", name="Probe", tools_opt_in=[SEARCH, AGGREGATE])

    assert {SEARCH, AGGREGATE} <= set(registry.get_tool_names(config))


def test_tool_search_finds_them_by_the_words_an_agent_uses():
    from robothor.engine.tools.keywords import TOOL_HINTS

    for name in (SEARCH, AGGREGATE):
        words = set(TOOL_HINTS[name].keywords)
        assert {"datadog", "logs", "errors"} <= words


# ── cross-tenant: a parent may read a child; a child never a sibling ───────


def _session() -> SimpleNamespace:
    run = SimpleNamespace(
        parent_run_id=None, nesting_depth=0, trigger_type=TriggerType.WEBCHAT, is_benchmark=False
    )
    return SimpleNamespace(run=run)


@pytest.fixture
def dispatch_env(dd):
    from robothor.engine.tools.handlers import datadog_logs

    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    with (
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=datadog_logs.HANDLERS),
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


def test_both_tools_may_cross_tenants_as_reads():
    from robothor.engine.tools.constants import CROSS_TENANT_READS

    assert {SEARCH, AGGREGATE} <= CROSS_TENANT_READS


def test_a_parent_naming_a_child_reads_with_the_childs_scope(dispatch_env):
    result = _dispatch(SEARCH, {"tenant": CHILD_A, "query": "status:error"})

    assert "error" not in result
    (body,) = dispatch_env.dd.bodies()
    assert body["filter"]["query"] == "(service:shop-api) AND (status:error)"
    assert result["scope"] == CHILD_A
    dispatch_env.audit.assert_called_once()


def test_a_child_naming_a_sibling_is_refused_as_unreachable(dispatch_env):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    result = _dispatch(
        AGGREGATE,
        {"tenant": CHILD_B, "group_by": ["service"]},
        tenant_id=CHILD_A,
        user_role="member",
        accessible_tenant_ids=(CHILD_A,),
    )

    assert result["refused"] is True
    assert result["error"] == UNREACHABLE
    assert dispatch_env.dd.requests == []


def test_the_schemas_advertise_the_tenant_argument(registry):
    for name in (SEARCH, AGGREGATE):
        props = registry._schemas[name]["function"]["parameters"]["properties"]
        assert "tenant" in props


# ── off the loop ───────────────────────────────────────────────────────────


async def test_the_client_is_built_off_the_event_loop(scopes, monkeypatch):
    """Building the client reads the vault, which blocks, so it runs in a worker thread.

    Checked directly rather than by timing: the shared 200 ms probe asserts a
    loop callback ran within 20 ms, which a saturated machine can miss with
    nothing on the loop at all. The factory records the thread it ran on and
    whether that thread was running an event loop; on the loop it would be
    this test's own thread.
    """
    from robothor.engine.tools.handlers import datadog_logs
    from robothor.integrations.datadog.client import AsyncDatadogClient

    fake = FakeDatadog()
    client = AsyncDatadogClient(API_KEY, APP_KEY, transport=httpx.MockTransport(fake.handler))
    seen: list[tuple[int, bool]] = []

    def factory() -> AsyncDatadogClient:
        try:
            asyncio.get_running_loop()
            on_a_loop = True
        except RuntimeError:
            on_a_loop = False
        seen.append((threading.get_ident(), on_a_loop))
        return client

    monkeypatch.setattr(datadog_logs, "_client_factory", factory)

    with patch("robothor.engine.event_writer.get_event_writer", return_value=MagicMock()):
        result = await datadog_logs.HANDLERS[SEARCH]({"limit": 1}, _ctx())

    assert "error" not in result
    assert len(seen) == 1, "the client factory was not called exactly once"
    thread, on_a_loop = seen[0]
    assert thread != threading.get_ident(), "the client was built on the event loop's thread"
    assert on_a_loop is False
