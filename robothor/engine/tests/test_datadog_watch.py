"""Genus watches Datadog: error spikes per tenant service, and the link itself.

Decision functions are pure and tested by their rule. The wiring is driven
through the real :class:`AsyncDatadogClient` over an ``httpx.MockTransport``,
so the queries asserted are the ones that would leave the process.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robothor.engine import datadog_watch as watch
from robothor.engine import detectors
from robothor.engine.event_writer import get_event_writer
from robothor.integrations.datadog.client import AsyncDatadogClient, DatadogError
from robothor.integrations.datadog.scope import DatadogScope

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


# ── wiring ──────────────────────────────────────────────────────────────────


class Datadog:
    """A MockTransport handler: answers aggregates per (scope, window), validates."""

    def __init__(
        self,
        *,
        current: dict[str, dict[str, int]] | None = None,
        baseline: dict[str, dict[str, int]] | None = None,
        fail: dict[str, int] | None = None,
        validate_status: int = 200,
        app_key_status: int = 200,
    ) -> None:
        self.current = current or {}
        self.baseline = baseline or {}
        self.fail = fail or {}
        self.validate_status = validate_status
        self.app_key_status = app_key_status
        self.queries: list[dict[str, Any]] = []
        self.validations = 0
        self.app_key_probes = 0
        self.current_start = CURRENT_START

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/validate":
            self.validations += 1
            return httpx.Response(self.validate_status, json={"valid": True})
        body = json.loads(request.content)
        if request.url.path == "/api/v2/logs/events/search":
            self.app_key_probes += 1
            return httpx.Response(self.app_key_status, json={"data": []})
        self.queries.append(body)
        query = body["filter"]["query"]
        for scope_query, status in self.fail.items():
            if scope_query in query:
                return httpx.Response(status, json={"errors": [SECRET_BODY]})
        counts = self.current if body["filter"]["from"] == self.current_start else self.baseline
        service = next((s for s in counts if f"service:{s}" in query), None)
        buckets = []
        if service is not None:
            buckets.append({"by": {"service": service}, "computes": {"c0": counts[service]}})
        return httpx.Response(200, json={"data": {"buckets": buckets}})


def _client(dd: Datadog) -> AsyncDatadogClient:
    return AsyncDatadogClient("api-key-0123", "app-key-4567", transport=httpx.MockTransport(dd))


def _watch(dd: Datadog, *, home: str = CHILD_A, root: bool = False) -> watch.DatadogWatch:
    async def factory() -> AsyncDatadogClient:
        return _client(dd)

    async def is_root(_tenant: str) -> bool:
        return root

    return watch.DatadogWatch(
        home_tenant=home,
        client_factory=factory,
        is_root=is_root,
        clock=lambda: NOW,
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
    per_tenant = 1 + watch.BASELINE_WINDOWS
    assert len(queries) == 2 * per_tenant
    assert queries.count("(service:shop-api) AND (status:error)") == per_tenant
    assert queries.count("(service:clinic-app) AND (status:error)") == per_tenant
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
