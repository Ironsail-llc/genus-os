"""Genus watches Datadog: error spikes per tenant service, and the link itself.

Decision functions are pure and tested by their rule. The wiring is driven
through the real :class:`AsyncDatadogClient` over an ``httpx.MockTransport``,
so the queries asserted are the ones that would leave the process.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robothor.engine import datadog_watch as watch
from robothor.engine import detectors
from robothor.engine.event_writer import get_event_writer
from robothor.integrations.datadog.client import AsyncDatadogClient, DatadogError
from robothor.integrations.datadog.scope import NO_ACCESS_QUERY, DatadogScope

if TYPE_CHECKING:
    from collections.abc import Callable

NOW = datetime(2026, 11, 25, 12, 7, tzinfo=UTC)
#: With NOW above: the latest complete 15-minute slot, and the eight before it.
CURRENT_START = "2026-11-25T11:45:00Z"
CURRENT_END = "2026-11-25T12:00:00Z"
FIRST_BASELINE_START = "2026-11-25T09:45:00Z"

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
SCOPES = {
    CHILD_A: DatadogScope(CHILD_A, "service:shop-api", "service:shop-api", "", ()),
    CHILD_B: DatadogScope(CHILD_B, "service:clinic-app", "service:clinic-app", "", ()),
}
#: A log message body that must never reach an event, a brief or a log line.
SECRET_BODY = "card 4111 declined for agent@example.com"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    detectors._dedup.clear()
    watch.reset_state()
    monkeypatch.setattr(watch, "scope_for", lambda tenant: SCOPES.get(tenant))
    yield
    detectors._dedup.clear()
    watch.reset_state()


# ── the spike rule ──────────────────────────────────────────────────────────

STEADY = [10, 12, 8, 11, 9, 10, 13, 10]


def test_a_spike_well_above_the_baseline_fires() -> None:
    found = watch.check_error_spike({"shop-api": 80}, {"shop-api": STEADY})
    assert [f.service for f in found] == ["shop-api"]
    assert found[0].current == 80
    assert found[0].baseline_median == 10
    assert found[0].new_service is False


def test_normal_variation_does_not_fire() -> None:
    for current in (8, 13, 20, 25):
        assert watch.check_error_spike({"shop-api": current}, {"shop-api": STEADY}) == []


def test_a_noisy_service_needs_a_bigger_jump() -> None:
    noisy = [5, 40, 6, 35, 4, 38, 5, 36]
    assert watch.check_error_spike({"shop-api": 90}, {"shop-api": noisy}) == []
    assert watch.check_error_spike({"shop-api": 400}, {"shop-api": noisy}) != []


def test_tiny_counts_do_not_fire_whatever_the_ratio() -> None:
    low = [2, 3, 2, 3, 2, 3, 2, 3]
    assert watch.check_error_spike({"shop-api": watch.SPIKE_MIN_COUNT - 1}, {"shop-api": low}) == []
    assert watch.check_error_spike({"shop-api": watch.SPIKE_MIN_COUNT}, {"shop-api": low}) != []


@pytest.mark.parametrize("stray", [[0, 0, 0, 0, 0, 0, 0, 1], [0, 1, 0, 0, 1, 0, 0, 0]])
def test_a_usually_silent_service_uses_the_higher_floor(stray: list[int]) -> None:
    """A stray error or two in the baseline is no usual level: no jump at zero."""
    below = watch.NEW_SERVICE_MIN_COUNT - 1
    assert watch.check_error_spike({"shop-api": watch.SPIKE_MIN_COUNT}, {"shop-api": stray}) == []
    assert watch.check_error_spike({"shop-api": below}, {"shop-api": stray}) == []
    assert watch.check_error_spike({"shop-api": below + 1}, {"shop-api": stray}) != []


def test_a_service_with_no_baseline_uses_the_higher_floor() -> None:
    below = watch.NEW_SERVICE_MIN_COUNT - 1
    assert watch.NEW_SERVICE_MIN_COUNT > watch.SPIKE_MIN_COUNT
    assert watch.check_error_spike({"clinic-app": below}, {}) == []
    assert watch.check_error_spike({"clinic-app": below}, {"clinic-app": [0] * 8}) == []
    found = watch.check_error_spike({"clinic-app": watch.NEW_SERVICE_MIN_COUNT}, {})
    assert [(f.service, f.new_service) for f in found] == [("clinic-app", True)]


# ── the connection rule ─────────────────────────────────────────────────────


def test_connection_lost_needs_consecutive_failures() -> None:
    n = watch.CONNECTION_FAILURES_FOR_LOST
    assert n >= 2
    assert watch.check_connection(["auth"] * (n - 1), lost=False) == "none"
    assert watch.check_connection(["ok"] + ["network"] * n, lost=False) == "lost"
    assert watch.check_connection(["auth", "ok"] + ["auth"] * (n - 1), lost=False) == "none"
    # A rate limit is not a lost connection, and breaks the streak.
    assert watch.check_connection(["auth"] * (n - 1) + ["rate_limit"], lost=False) == "none"


def test_connection_restores_on_the_first_success_and_only_once() -> None:
    n = watch.CONNECTION_FAILURES_FOR_LOST
    assert watch.check_connection(["auth"] * n, lost=True) == "none"
    assert watch.check_connection(["auth"] * n + ["ok"], lost=True) == "restored"
    assert watch.check_connection(["ok", "ok"], lost=False) == "none"


# ── the monitor rule ────────────────────────────────────────────────────────


def _changes(firing: dict[str, str], recorded: dict[str, str]) -> list[tuple[Any, ...]]:
    return [
        (c.monitor_id, c.kind, c.status, c.previous) for c in watch.check_monitors(firing, recorded)
    ]


def test_a_newly_firing_monitor_is_a_change() -> None:
    assert _changes({"9": "alert"}, {}) == [("9", "firing", "alert", None)]
    assert _changes({"9": "no data"}, {}) == [("9", "firing", "no data", None)]


def test_a_monitor_still_firing_at_the_same_status_is_not_a_change() -> None:
    assert _changes({"9": "alert"}, {"9": "alert"}) == []


def test_a_status_change_while_firing_is_a_change() -> None:
    assert _changes({"9": "alert"}, {"9": "warn"}) == [("9", "firing", "alert", "warn")]


def test_a_recorded_monitor_that_stops_firing_has_recovered() -> None:
    assert _changes({}, {"9": "warn"}) == [("9", "recovered", None, "warn")]
    assert _changes({"7": "alert"}, {"7": "alert", "9": "alert"}) == [
        ("9", "recovered", None, "alert")
    ]


# ── the silence rule ────────────────────────────────────────────────────────

BUSY = [200, 180, 220, 210, 190, 205, 199, 200]


def _silence(
    current: dict[str, int], baseline: dict[str, list[int]], silent: set[str] | None = None
) -> list[tuple[str, str, int]]:
    return [
        (c.service, c.kind, c.current)
        for c in watch.check_silence(current, baseline, silent or set())
    ]


def test_a_steady_service_that_logs_nothing_is_silent() -> None:
    assert len(BUSY) == watch.BASELINE_WINDOWS
    assert _silence({}, {"shop-api": BUSY}) == [("shop-api", "silent", 0)]
    assert _silence({"shop-api": 0}, {"shop-api": BUSY}) == [("shop-api", "silent", 0)]
    (found,) = watch.check_silence({}, {"shop-api": BUSY}, set())
    assert found.baseline_median == 200


def test_a_service_still_logging_is_not_silent() -> None:
    assert _silence({"shop-api": 1}, {"shop-api": BUSY}) == []


def test_a_naturally_quiet_service_is_never_silent() -> None:
    floor = watch.SILENT_MIN_MEDIAN
    assert _silence({}, {"shop-api": [floor - 1] * 8}) == []
    assert _silence({}, {"shop-api": [floor] * 8}) == [("shop-api", "silent", 0)]


def test_a_service_logging_once_a_minute_can_go_silent() -> None:
    """The floor is one line a minute: a service that logs every minute is watched."""
    per_slot = watch.WINDOW_MINUTES
    assert _silence({}, {"shop-api": [per_slot] * 8}) == [("shop-api", "silent", 0)]
    assert _silence({}, {"shop-api": [per_slot - 1] * 8}) == []


def test_a_service_with_a_gap_in_its_baseline_is_not_silent() -> None:
    """Logging in every baseline slot is the "steadily" in "logging steadily"."""
    gappy = [200, 200, 0, 200, 200, 200, 200, 200]
    assert _silence({}, {"shop-api": gappy}) == []


def test_a_service_without_a_full_baseline_is_not_silent() -> None:
    assert _silence({}, {}) == []
    assert _silence({}, {"clinic-app": [200] * (watch.BASELINE_WINDOWS - 1)}) == []


def test_logs_without_a_service_are_not_a_silent_service() -> None:
    assert _silence({}, {"(no service)": BUSY}) == []


def test_a_silent_service_is_found_once_and_resumes_when_it_logs() -> None:
    assert _silence({}, {"shop-api": BUSY}, {"shop-api"}) == []
    assert _silence({"shop-api": 5}, {"shop-api": [0] + BUSY[1:]}, {"shop-api"}) == [
        ("shop-api", "resumed", 5)
    ]
    # Still silent, even once the silence fills the baseline: nothing new.
    assert _silence({}, {"shop-api": [0] * 8}, {"shop-api"}) == []


# ── wiring ──────────────────────────────────────────────────────────────────


class Datadog:
    """A MockTransport handler: aggregates per (scope, window), monitors, validates.

    ``current``/``baseline`` answer the ``status:error`` series; ``totals_current``
    /``totals_baseline`` answer the every-line series. ``monitors`` are the rows
    the monitor search returns, paged by the request's ``page``/``per_page``.
    """

    def __init__(
        self,
        *,
        current: dict[str, int] | None = None,
        baseline: dict[str, int] | None = None,
        totals_current: dict[str, int] | None = None,
        totals_baseline: dict[str, int] | None = None,
        monitors: list[dict[str, Any]] | None = None,
        monitor_status: int = 200,
        fail: dict[str, int] | None = None,
        validate_status: int = 200,
        app_key_status: int = 200,
    ) -> None:
        self.current = current or {}
        self.baseline = baseline or {}
        self.totals_current = totals_current or {}
        self.totals_baseline = totals_baseline or {}
        self.monitors = monitors or []
        self.monitor_status = monitor_status
        self.fail = fail or {}
        self.validate_status = validate_status
        self.app_key_status = app_key_status
        self.queries: list[dict[str, Any]] = []
        self.monitor_requests: list[dict[str, str]] = []
        self.validations = 0
        self.app_key_probes = 0
        self.current_start = CURRENT_START

    def _monitor_search(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.monitor_requests.append(params)
        if self.monitor_status != 200:
            return httpx.Response(self.monitor_status, json={"errors": [SECRET_BODY]})
        page, per_page = int(params["page"]), int(params["per_page"])
        rows = self.monitors[page * per_page : (page + 1) * per_page]
        page_count = -(-len(self.monitors) // per_page)
        meta = {"total_count": len(self.monitors), "page": page, "page_count": page_count}
        return httpx.Response(200, json={"monitors": rows, "metadata": meta})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/validate":
            self.validations += 1
            return httpx.Response(self.validate_status, json={"valid": True})
        if request.url.path == "/api/v1/monitor/search":
            return self._monitor_search(request)
        body = json.loads(request.content)
        if request.url.path == "/api/v2/logs/events/search":
            self.app_key_probes += 1
            return httpx.Response(self.app_key_status, json={"data": []})
        self.queries.append(body)
        query = body["filter"]["query"]
        for scope_query, status in self.fail.items():
            if scope_query in query:
                return httpx.Response(status, json={"errors": [SECRET_BODY]})
        is_current = body["filter"]["from"] == self.current_start
        if "status:error" in query:
            counts = self.current if is_current else self.baseline
        else:
            counts = self.totals_current if is_current else self.totals_baseline
        # A service-scoped query sees its own service; the root scope sees all.
        if "service:" in query:
            services = [s for s in counts if f"service:{s}" in query]
        else:
            services = list(counts)
        buckets = [{"by": {"service": s}, "computes": {"c0": counts[s]}} for s in services]
        return httpx.Response(200, json={"data": {"buckets": buckets}})


def _client(dd: Datadog) -> AsyncDatadogClient:
    return AsyncDatadogClient("api-key-0123", "app-key-4567", transport=httpx.MockTransport(dd))


def _watch(
    dd: Datadog,
    *,
    home: str = CHILD_A,
    root: bool = False,
    clock: Callable[[], datetime] = lambda: NOW,
) -> watch.DatadogWatch:
    async def factory() -> AsyncDatadogClient:
        return _client(dd)

    async def is_root(_tenant: str) -> bool:
        return root

    return watch.DatadogWatch(
        home_tenant=home,
        client_factory=factory,
        is_root=is_root,
        clock=clock,
    )


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    recorder = MagicMock(return_value={"id": 1, "timestamp": NOW.isoformat()})
    monkeypatch.setattr("robothor.audit.logger.log_event", recorder)
    return recorder


@pytest.fixture
def alert(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr("robothor.engine.alerts.alert", sender)
    return sender


async def _events(audit: MagicMock) -> list[dict[str, Any]]:
    await get_event_writer().flush()
    out = []
    for call in audit.call_args_list:
        assert call.args[0] == "datadog.watch_finding"
        out.append({"tenant_id": call.kwargs["tenant_id"], **call.kwargs["details"]})
    return out


SPIKING = {
    "current": {"shop-api": 80, "clinic-app": 90},
    "baseline": {"shop-api": 10, "clinic-app": 10},
}


async def test_each_tenant_gets_its_own_scoped_queries(audit: MagicMock, alert: AsyncMock) -> None:
    dd = Datadog()
    await _watch(dd).tick([CHILD_A, CHILD_B])
    queries = [q["filter"]["query"] for q in dd.queries]
    per_series = 1 + watch.BASELINE_WINDOWS
    # Two series per tenant: error lines (spikes) and every line (silence).
    assert len(queries) == 2 * 2 * per_series
    assert queries.count("(service:shop-api) AND (status:error)") == per_series
    assert queries.count("(service:clinic-app) AND (status:error)") == per_series
    assert queries.count("service:shop-api") == per_series
    assert queries.count("service:clinic-app") == per_series
    assert all(q["group_by"][0]["facet"] == "service" for q in dd.queries)
    windows = {(q["filter"]["from"], q["filter"]["to"]) for q in dd.queries}
    assert (CURRENT_START, CURRENT_END) in windows
    assert min(start for start, _ in windows) == FIRST_BASELINE_START


async def test_a_settled_window_is_not_asked_for_again(audit: MagicMock, alert: AsyncMock) -> None:
    dd = Datadog()
    w = _watch(dd)
    await w.tick([CHILD_A])
    first = len(dd.queries)
    await w.tick([CHILD_A])
    assert len(dd.queries) == first


async def test_a_tenant_without_scope_is_skipped_and_logged_once(
    audit: MagicMock, alert: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    dd = Datadog(**SPIKING)
    w = _watch(dd)
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        await w.tick(["acme-unscoped", CHILD_A])
        await w.tick(["acme-unscoped", CHILD_A])
    assert all("acme-unscoped" not in json.dumps(q) for q in dd.queries)
    mentions = [r for r in caplog.records if "acme-unscoped" in r.getMessage()]
    assert len(mentions) == 1
    assert [e["tenant_id"] for e in await _events(audit)] == [CHILD_A]


async def test_an_error_for_one_tenant_does_not_stop_the_other(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(**SPIKING, fail={"service:shop-api": 500})
    await _watch(dd).tick([CHILD_A, CHILD_B])
    events = await _events(audit)
    assert [(e["tenant_id"], e["service"]) for e in events] == [(CHILD_B, "clinic-app")]


async def test_dry_run_records_the_finding_and_delivers_nothing(
    audit: MagicMock, alert: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    dd = Datadog(**SPIKING)
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        found = await _watch(dd).tick([CHILD_A])
    assert found == 1
    alert.assert_not_called()
    (event,) = await _events(audit)
    assert event["tenant_id"] == CHILD_A
    assert event["kind"] == "error_spike"
    assert event["service"] == "shop-api"
    assert event["current"] == 80
    assert event["baseline"]["median"] == 10
    assert event["window"] == {"start": CURRENT_START, "end": CURRENT_END, "minutes": 15}
    assert event["mode"] == "dry_run"
    assert CHILD_A in event["would_notify"]
    assert "shop-api" in event["brief"] and "80" in event["brief"]
    assert event["brief"].count(". ") + 1 <= 3
    lines = [r.getMessage() for r in caplog.records if "event=datadog_watch" in r.getMessage()]
    assert len(lines) == 1
    for field in (f"tenant={CHILD_A}", "kind=error_spike", "service=shop-api", "mode=dry_run"):
        assert field in lines[0]


async def test_dedup_holds_across_ticks(audit: MagicMock, alert: AsyncMock) -> None:
    dd = Datadog(**SPIKING)
    w = _watch(dd)
    assert await w.tick([CHILD_A]) == 1
    assert await w.tick([CHILD_A]) == 0
    assert len(await _events(audit)) == 1


async def test_no_log_body_reaches_an_event_or_a_line(
    audit: MagicMock, alert: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    dd = Datadog(**SPIKING, fail={"service:clinic-app": 403})
    with caplog.at_level(logging.DEBUG):
        await _watch(dd).tick([CHILD_A, CHILD_B])
    assert await _events(audit)
    assert SECRET_BODY not in json.dumps([c.kwargs for c in audit.call_args_list], default=str)
    assert SECRET_BODY not in json.dumps([c.args for c in alert.call_args_list], default=str)
    assert SECRET_BODY not in caplog.text


# ── the connection watch ────────────────────────────────────────────────────


async def test_only_the_root_engine_watches_the_connection(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(validate_status=403)
    child = _watch(dd, home=CHILD_A, root=False)
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST + 1):
        await child.tick([CHILD_A])
    assert dd.validations == 0
    assert await _events(audit) == []


async def test_connection_lost_then_restored_in_the_root_tenant(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(validate_status=401)
    w = _watch(dd, home=PARENT, root=True)
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST + 2):
        await w.tick([PARENT])
    events = await _events(audit)
    assert [(e["tenant_id"], e["kind"]) for e in events] == [(PARENT, "connection_lost")]
    assert events[0]["mode"] == "dry_run" and events[0]["brief"]
    dd.validate_status = 200
    await w.tick([PARENT])
    await w.tick([PARENT])
    kinds = [e["kind"] for e in await _events(audit)]
    assert kinds == ["connection_lost", "connection_restored"]
    alert.assert_not_called()


async def test_a_network_failure_on_the_root_tenants_reads_counts(
    audit: MagicMock, alert: AsyncMock
) -> None:
    SCOPES[PARENT] = DatadogScope(PARENT, "*", "*", "", ())
    try:
        dd = Datadog(fail={"status:error": 503})
        w = _watch(dd, home=PARENT, root=True)
        for _ in range(watch.CONNECTION_FAILURES_FOR_LOST):
            await w.tick([PARENT])
        assert [e["kind"] for e in await _events(audit)] == ["connection_lost"]
    finally:
        del SCOPES[PARENT]


async def test_a_revoked_application_key_is_a_lost_link(audit: MagicMock, alert: AsyncMock) -> None:
    """``/api/v1/validate`` checks only the API key; the probe checks the other one."""
    dd = Datadog(app_key_status=403)
    w = _watch(dd, home=PARENT, root=True)
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST):
        await w.tick([PARENT])
    assert dd.validations == watch.CONNECTION_FAILURES_FOR_LOST
    assert dd.app_key_probes == watch.CONNECTION_FAILURES_FOR_LOST
    events = await _events(audit)
    assert [e["kind"] for e in events] == ["connection_lost"]
    assert "application key" in events[0]["brief"]


async def test_an_unconfigured_client_watches_nothing(audit: MagicMock, alert: AsyncMock) -> None:
    async def factory() -> AsyncDatadogClient:
        raise DatadogError("not_configured")

    async def is_root(_tenant: str) -> bool:
        return True

    w = watch.DatadogWatch(
        home_tenant=PARENT, client_factory=factory, is_root=is_root, clock=lambda: NOW
    )
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST + 1):
        assert await w.tick([PARENT, CHILD_A]) == 0
    assert await _events(audit) == []


# ── the monitor watch ───────────────────────────────────────────────────────

#: Firing, and not muted: Datadog holds back a muted monitor's own notification.
FIRING_QUERY = '(status:alert OR status:warn OR status:"no data") AND muted:false'


def _monitor(
    monitor_id: int, status: str = "Alert", *, name: str = "", ago: timedelta | None = None
) -> dict[str, Any]:
    since = NOW - (ago if ago is not None else timedelta(hours=2))
    return {
        "id": monitor_id,
        "name": name or f"shop-api error rate {monitor_id}",
        "status": status,
        "type": "log alert",
        "tags": ["team:shop"],
        "last_triggered_ts": int(since.timestamp()),
    }


@pytest.fixture
def monitor_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHILD_A may read its team's monitors; CHILD_B has no monitor access."""
    monkeypatch.setitem(
        SCOPES, CHILD_A, DatadogScope(CHILD_A, "service:shop-api", "*", "team:shop", ())
    )
    monkeypatch.setitem(
        SCOPES,
        CHILD_B,
        DatadogScope(CHILD_B, "service:clinic-app", "*", NO_ACCESS_QUERY, ()),
    )


def _monitor_events(events: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (e["kind"], e["monitor"]["id"], e["monitor"]["status"], e["monitor"]["previous_status"])
        for e in events
        if e["kind"].startswith("monitor_")
    ]


async def test_the_monitor_query_is_the_tenants_scope_and_firing_statuses(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    dd = Datadog(monitors=[_monitor(9)])
    await _watch(dd).tick([CHILD_A])
    assert [r["query"] for r in dd.monitor_requests] == [f"(team:shop) AND ({FIRING_QUERY})"]
    assert dd.monitor_requests[0]["page"] == "0"


async def test_a_firing_monitor_is_recorded_once_with_a_brief(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None, caplog: pytest.LogCaptureFixture
) -> None:
    dd = Datadog(monitors=[_monitor(9, "Warn", name="checkout errors for agent@example.com")])
    w = _watch(dd)
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        assert await w.tick([CHILD_A]) == 1
        assert await w.tick([CHILD_A]) == 0
    (event,) = await _events(audit)
    assert event["tenant_id"] == CHILD_A
    assert event["kind"] == "monitor_firing"
    assert event["mode"] == "dry_run"
    assert event["monitor"]["id"] == "9"
    assert event["monitor"]["status"] == "warn"
    assert event["monitor"]["previous_status"] is None
    assert "agent@example.com" not in json.dumps(event)
    assert "<email>" in event["brief"] and "checkout errors" in event["brief"]
    assert "Warn" in event["brief"] and "2 hours" in event["brief"]
    assert CHILD_A in event["would_notify"]
    lines = [r.getMessage() for r in caplog.records if "event=datadog_watch" in r.getMessage()]
    assert len(lines) == 1
    assert "kind=monitor_firing" in lines[0] and "mode=dry_run" in lines[0]
    assert "agent@example.com" not in caplog.text


async def test_a_status_change_is_recorded_again(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    dd = Datadog(monitors=[_monitor(9, "Warn")])
    w = _watch(dd)
    await w.tick([CHILD_A])
    dd.monitors = [_monitor(9, "Alert")]
    await w.tick([CHILD_A])
    await w.tick([CHILD_A])
    assert _monitor_events(await _events(audit)) == [
        ("monitor_firing", "9", "warn", None),
        ("monitor_firing", "9", "alert", "warn"),
    ]


async def test_recovery_is_recorded_and_a_new_firing_is_recorded_again(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    dd = Datadog(monitors=[_monitor(9)])
    w = _watch(dd)
    await w.tick([CHILD_A])
    dd.monitors = []
    await w.tick([CHILD_A])
    await w.tick([CHILD_A])
    dd.monitors = [_monitor(9)]
    await w.tick([CHILD_A])
    events = await _events(audit)
    assert _monitor_events(events) == [
        ("monitor_firing", "9", "alert", None),
        ("monitor_recovered", "9", None, "alert"),
        ("monitor_firing", "9", "alert", None),
    ]
    recovered = events[1]
    assert recovered["mode"] == "dry_run"
    assert "shop-api error rate 9" in recovered["brief"]
    alert.assert_not_called()


async def test_a_failed_monitor_read_is_not_a_recovery(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    dd = Datadog(monitors=[_monitor(9)])
    w = _watch(dd)
    await w.tick([CHILD_A])
    dd.monitor_status = 500
    await w.tick([CHILD_A])
    dd.monitor_status = 200
    await w.tick([CHILD_A])
    assert _monitor_events(await _events(audit)) == [("monitor_firing", "9", "alert", None)]


async def test_every_page_of_firing_monitors_is_read(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    count = 2 * watch.MONITOR_PAGE + 1
    dd = Datadog(monitors=[_monitor(i) for i in range(count)])
    assert await _watch(dd).tick([CHILD_A]) == count
    assert [r["page"] for r in dd.monitor_requests] == ["0", "1", "2"]
    assert {r["per_page"] for r in dd.monitor_requests} == {str(watch.MONITOR_PAGE)}
    assert len({e["monitor"]["id"] for e in await _events(audit)}) == count


async def test_a_tenant_without_monitor_access_skips_only_that_observer(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None, caplog: pytest.LogCaptureFixture
) -> None:
    dd = Datadog(**SPIKING, monitors=[_monitor(9)])
    w = _watch(dd)
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        await w.tick([CHILD_B, CHILD_A])
        await w.tick([CHILD_B, CHILD_A])
    # Only CHILD_A asked for monitors; CHILD_B's spike was still found.
    assert len(dd.monitor_requests) == 2
    assert all("team:shop" in r["query"] for r in dd.monitor_requests)
    kinds = {(e["tenant_id"], e["kind"]) for e in await _events(audit)}
    assert (CHILD_B, "error_spike") in kinds
    assert (CHILD_A, "monitor_firing") in kinds
    assert not any(t == CHILD_B and k.startswith("monitor_") for t, k in kinds)
    mentions = [
        r for r in caplog.records if CHILD_B in r.getMessage() and "monitor" in r.getMessage()
    ]
    assert len(mentions) == 1


async def test_a_monitor_permission_refusal_is_not_a_lost_link(
    audit: MagicMock, alert: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An application key without monitor read access still reads logs."""
    monkeypatch.setitem(SCOPES, PARENT, DatadogScope(PARENT, "*", "*", "*", ()))
    dd = Datadog(monitor_status=403)
    w = _watch(dd, home=PARENT, root=True)
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST + 1):
        await w.tick([PARENT])
    assert await _events(audit) == []
    # The root scope's monitor query is the firing statuses alone.
    assert dd.monitor_requests[0]["query"] == FIRING_QUERY


async def test_an_unreachable_monitor_search_counts_toward_a_lost_link(
    audit: MagicMock, alert: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike a permission refusal, a 5xx from the monitor search is the link failing."""
    monkeypatch.setitem(SCOPES, PARENT, DatadogScope(PARENT, "*", "*", "*", ()))
    dd = Datadog(monitor_status=503)
    w = _watch(dd, home=PARENT, root=True)
    for _ in range(watch.CONNECTION_FAILURES_FOR_LOST):
        await w.tick([PARENT])
    assert [e["kind"] for e in await _events(audit)] == ["connection_lost"]


# ── the silent-service watch ────────────────────────────────────────────────

NEXT_START = "2026-11-25T12:00:00Z"


async def test_a_silent_service_is_recorded_once_then_resumes(
    audit: MagicMock, alert: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    now = [NOW]
    dd = Datadog(totals_baseline={"shop-api": 200})
    w = _watch(dd, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        assert await w.tick([CHILD_A]) == 1
        now[0] = NOW + timedelta(minutes=5)
        assert await w.tick([CHILD_A]) == 0
    (silent,) = await _events(audit)
    assert silent["kind"] == "service_silent"
    assert silent["mode"] == "dry_run"
    assert silent["service"] == "shop-api"
    assert silent["current"] == 0
    assert silent["baseline"]["median"] == 200
    assert silent["window"] == {"start": CURRENT_START, "end": CURRENT_END, "minutes": 15}
    assert "shop-api" in silent["brief"] and "200" in silent["brief"]
    lines = [r.getMessage() for r in caplog.records if "event=datadog_watch" in r.getMessage()]
    assert len(lines) == 1 and "kind=service_silent" in lines[0]

    # The next slot: shop-api logs again.
    now[0] = NOW + timedelta(minutes=15)
    dd.current_start = NEXT_START
    dd.totals_current = {"shop-api": 150}
    assert await w.tick([CHILD_A]) == 1
    events = await _events(audit)
    assert [e["kind"] for e in events] == ["service_silent", "service_resumed"]
    resumed = events[1]
    assert resumed["service"] == "shop-api" and resumed["current"] == 150
    assert resumed["mode"] == "dry_run"
    assert resumed["window"]["start"] == NEXT_START
    assert "150" in resumed["brief"]
    alert.assert_not_called()


async def test_a_quiet_service_logging_nothing_is_not_recorded(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(totals_baseline={"shop-api": watch.SILENT_MIN_MEDIAN - 1})
    assert await _watch(dd).tick([CHILD_A]) == 0
    assert await _events(audit) == []


async def test_a_service_logging_once_a_minute_is_recorded_silent(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(totals_baseline={"shop-api": watch.WINDOW_MINUTES})
    assert await _watch(dd).tick([CHILD_A]) == 1
    assert [(e["kind"], e["service"]) for e in await _events(audit)] == [
        ("service_silent", "shop-api")
    ]


async def test_a_full_page_of_services_is_not_judged_for_silence(
    audit: MagicMock,
    alert: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A steady service missing from a full group page may only not have fit."""
    monkeypatch.setitem(SCOPES, PARENT, DatadogScope(PARENT, "*", "*", "", ()))
    page = {f"svc-{i:04d}": 500 for i in range(watch.GROUP_PAGE)}
    dd = Datadog(totals_current=page, totals_baseline={**page, "shop-api": 200})
    w = _watch(dd, home=PARENT)
    with caplog.at_level(logging.INFO, logger=watch.__name__):
        await w.tick([PARENT])
        await w.tick([PARENT])
    assert await _events(audit) == []
    mentions = [r for r in caplog.records if "silence not judged" in r.getMessage()]
    assert len(mentions) == 1


async def test_silence_is_read_inside_each_tenants_scope(
    audit: MagicMock, alert: AsyncMock
) -> None:
    dd = Datadog(totals_baseline={"shop-api": 200, "clinic-app": 200})
    await _watch(dd).tick([CHILD_A, CHILD_B])
    events = await _events(audit)
    assert sorted((e["tenant_id"], e["service"]) for e in events) == [
        (CHILD_A, "shop-api"),
        (CHILD_B, "clinic-app"),
    ]


async def test_a_failed_silence_read_does_not_stop_the_spike_watch(
    audit: MagicMock, alert: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, *args: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(watch.DatadogWatch, "_watch_silence", broken)
    dd = Datadog(**SPIKING)
    await _watch(dd).tick([CHILD_A])
    assert [e["kind"] for e in await _events(audit)] == ["error_spike"]


async def test_every_new_kind_is_dry_run_and_nothing_is_delivered(
    audit: MagicMock, alert: AsyncMock, monitor_scopes: None
) -> None:
    now = [NOW]
    dd = Datadog(totals_baseline={"shop-api": 200}, monitors=[_monitor(9)])
    w = _watch(dd, clock=lambda: now[0])
    await w.tick([CHILD_A])
    now[0] = NOW + timedelta(minutes=15)
    dd.current_start = NEXT_START
    dd.totals_current = {"shop-api": 150}
    dd.monitors = []
    await w.tick([CHILD_A])
    events = await _events(audit)
    assert {e["kind"] for e in events} == {
        "monitor_firing",
        "monitor_recovered",
        "service_silent",
        "service_resumed",
    }
    assert all(e["mode"] == "dry_run" and e["brief"] and e["would_notify"] for e in events)
    assert SECRET_BODY not in json.dumps(events)
    alert.assert_not_called()


# ── no mode setting, and the daemon hook ────────────────────────────────────


def test_there_is_no_mode_setting() -> None:
    """Recording is the only code path; delivery arrives as its own change."""
    from robothor.settings.model import GenusSettings, IntegrationSettings

    assert "datadog_watch_mode" not in IntegrationSettings.model_fields
    envs = {
        (field.json_schema_extra or {}).get("env")
        for group in GenusSettings.model_fields.values()
        if isinstance(group.annotation, type)
        for field in getattr(group.annotation, "model_fields", {}).values()
        if isinstance(field.json_schema_extra, dict)
    }
    assert "ROBOTHOR_DATADOG_SCOPES" in envs  # the scan sees the group
    assert "ROBOTHOR_DATADOG_WATCH_MODE" not in envs
    assert not hasattr(watch, "watch_mode")


async def test_no_finding_is_ever_delivered(audit: MagicMock, alert: AsyncMock) -> None:
    """Spikes and both link findings are recorded as dry run; nothing calls alerts.alert."""
    SCOPES[PARENT] = DatadogScope(PARENT, "service:shop-api", "*", "", ())
    try:
        dd = Datadog(**SPIKING, validate_status=401)
        w = _watch(dd, home=PARENT, root=True)
        for _ in range(watch.CONNECTION_FAILURES_FOR_LOST):
            await w.tick([PARENT])
        dd.validate_status = 200
        await w.tick([PARENT])
    finally:
        del SCOPES[PARENT]
    events = await _events(audit)
    assert {e["kind"] for e in events} == {"error_spike", "connection_lost", "connection_restored"}
    assert all(e["mode"] == "dry_run" for e in events)
    assert all(e["brief"] and e["would_notify"] for e in events)
    alert.assert_not_called()


async def test_the_daemon_runs_the_watch_in_the_background_one_pass_at_a_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A slow Datadog must not hold the watchdog loop (pings, approvals, reconcile)."""
    import asyncio

    from robothor.engine import daemon

    for name in ("repeat_error_detector", "tool_degradation_detector", "runaway_burn_detector"):
        monkeypatch.setattr(f"robothor.engine.detectors.{name}", AsyncMock(return_value=0))
    release = asyncio.Event()
    calls: list[str] = []

    async def hanging(tenant_id: str) -> int:
        calls.append(tenant_id)
        await release.wait()
        return 0

    monkeypatch.setattr("robothor.engine.datadog_watch.datadog_watch_detector", hanging)
    await asyncio.wait_for(daemon._run_tick_detectors(10, "test-tenant"), timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(daemon._run_tick_detectors(20, "test-tenant"), timeout=1)
    await asyncio.sleep(0)
    assert calls == ["test-tenant"]
    release.set()
    await watch.wait_for_pass()

    async def failing(tenant_id: str) -> int:
        raise RuntimeError("watch failed")

    monkeypatch.setattr("robothor.engine.datadog_watch.datadog_watch_detector", failing)
    with caplog.at_level(logging.WARNING):
        await daemon._run_tick_detectors(10, "test-tenant")
        await watch.wait_for_pass()
    assert any(
        "datadog watch" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records
    )


@pytest.mark.parametrize("gate", ["detectors_off", "follower"])
async def test_the_detector_is_gated(
    gate: str, monkeypatch: pytest.MonkeyPatch, audit: MagicMock
) -> None:
    factory = AsyncMock()
    monkeypatch.setattr(watch, "_default_client", factory)
    monkeypatch.setattr(watch, "_scope_map_declared", lambda: True)
    if gate == "detectors_off":
        monkeypatch.setenv("ROBOTHOR_DETECTORS_ENABLED", "0")
    else:
        monkeypatch.setattr("robothor.engine.leader.is_leader", lambda: False)
    assert await watch.datadog_watch_detector("test-tenant") == 0
    factory.assert_not_called()


async def test_the_detector_does_nothing_without_a_scope_map(
    monkeypatch: pytest.MonkeyPatch, audit: MagicMock
) -> None:
    factory = AsyncMock()
    monkeypatch.setattr(watch, "_default_client", factory)
    monkeypatch.delenv("ROBOTHOR_DATADOG_SCOPES", raising=False)
    from robothor.settings import reset_settings

    reset_settings()
    try:
        assert await watch.datadog_watch_detector("test-tenant") == 0
    finally:
        reset_settings()
    factory.assert_not_called()


# ── the root tenant ─────────────────────────────────────────────────────────


def _tenant_row(tenant: str, *, parent: str | None) -> dict[str, Any]:
    """What ``dal.get_tenant`` really returns: the ``tenant_to_dict`` shape."""
    from robothor.crm.models import tenant_to_dict

    return tenant_to_dict({"id": tenant, "display_name": tenant, "parent_tenant_id": parent})


@pytest.mark.parametrize(
    ("row", "root"),
    [
        (_tenant_row(CHILD_A, parent=PARENT), False),
        (_tenant_row(PARENT, parent=None), True),
        (None, True),
    ],
)
async def test_only_a_tenant_without_a_parent_is_the_root(
    row: dict[str, Any] | None, root: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("robothor.crm.dal.get_tenant", lambda _tenant: row)
    assert await watch._default_is_root(CHILD_A) is root


# ── no blocking call on the loop ────────────────────────────────────────────


async def test_the_tenant_tree_and_the_vault_are_read_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor

    await warm_executor()
    loop = asyncio.get_running_loop()
    tree = SlowProbe(loop, result=_tenant_row(PARENT, parent=None))
    vault = SlowProbe(loop, result=_client(Datadog()))
    monkeypatch.setattr("robothor.crm.dal.get_tenant", tree)
    monkeypatch.setattr("robothor.integrations.datadog.client.async_client_from_settings", vault)
    assert await watch._default_is_root(PARENT) is True
    await watch._default_client()
    for probe in (tree, vault):
        await probe.settle()
        probe.assert_loop_free()
