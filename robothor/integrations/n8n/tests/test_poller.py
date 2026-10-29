"""The poller: snapshot every round, notify once per failure, warn once per outage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from robothor.integrations.n8n import poller
from robothor.integrations.n8n.client import N8nClient
from robothor.integrations.n8n.store import MemoryN8nStore

BASE = "https://n8n.example.com"
KEY = "n8n-api-key-0123456789abcdef"
T = "test-tenant"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def configure(monkeypatch):
    from robothor.settings import get_settings, reset_settings

    def _set(**pairs: str):
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()
        return get_settings()

    for name in (
        "ROBOTHOR_N8N_ENABLED",
        "ROBOTHOR_N8N_BASE_URL",
        "ROBOTHOR_N8N_API_KEY",
        "ROBOTHOR_N8N_POLL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield _set
    reset_settings()


@pytest.fixture
def enabled(configure):
    return configure(
        ROBOTHOR_N8N_ENABLED="true",
        ROBOTHOR_N8N_BASE_URL=BASE,
        ROBOTHOR_N8N_POLL_SECONDS="120",
    )


class FakeN8n:
    """An n8n instance behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.workflows = [
            {"id": "wf1", "name": "Invoice sync", "active": True, "tags": []},
            {"id": "wf2", "name": "Lead router", "active": False, "tags": []},
        ]
        self.executions: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.fail_with: int | Exception | None = None

    def add_failure(self, eid: str, wid: str = "wf1", *, hours_ago: float = 1) -> None:
        at = (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
        self.executions.insert(
            0,
            {
                "id": eid,
                "workflowId": wid,
                "status": "error",
                "mode": "trigger",
                "startedAt": at,
                "stoppedAt": at,
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        if isinstance(self.fail_with, int):
            return httpx.Response(self.fail_with, json={})
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/v1/workflows":
            return httpx.Response(200, json={"data": self.workflows, "nextCursor": None})
        if path == "/api/v1/executions":
            return httpx.Response(200, json={"data": self.executions, "nextCursor": None})
        if path.startswith("/api/v1/executions/"):
            eid = path.rsplit("/", 1)[1]
            error = {"message": f"node exploded in {eid}"}
            return httpx.Response(200, json={"id": eid, "data": {"resultData": {"error": error}}})
        return httpx.Response(404, json={})

    def factory(self, _settings) -> N8nClient:
        return N8nClient(BASE, KEY, transport=httpx.MockTransport(self.handler))


class Recorder:
    def __init__(self) -> None:
        self.notes: list[dict] = []
        self.alerts: list[tuple[str, str, str]] = []
        self.sleeps: list[float] = []

    async def notify(self, tenant_id: str, subject: str, body: str, metadata: dict) -> None:
        self.notes.append(
            {"tenant_id": tenant_id, "subject": subject, "body": body, "metadata": metadata}
        )

    async def alert(self, level: str, title: str, body: str) -> None:
        self.alerts.append((level, title, body))

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def _run(n8n: FakeN8n, rec: Recorder, store: MemoryN8nStore, settings, **kw) -> None:
    kw.setdefault("is_leader", lambda: True)
    kw.setdefault("now", lambda: NOW)
    await poller.run(
        T,
        settings=settings,
        client_factory=n8n.factory,
        store=store,
        notify=rec.notify,
        alert=rec.alert,
        sleep=rec.sleep,
        **kw,
    )


def test_disabled_by_default(configure) -> None:
    configure()
    assert poller.n8n_enabled() is False
    assert poller.start_tasks(T) == []


async def test_disabled_run_touches_nothing(configure) -> None:
    settings = configure(ROBOTHOR_N8N_BASE_URL=BASE)
    n8n, rec = FakeN8n(), Recorder()
    await _run(n8n, rec, MemoryN8nStore(now=lambda: NOW), settings, rounds=2)
    assert n8n.requests == [] and rec.notes == [] and rec.sleeps == []


async def test_first_round_notifies_once_per_failure_and_the_second_is_silent(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    n8n.add_failure("101", "wf1")
    n8n.add_failure("102", "wf2")
    store = MemoryN8nStore(now=lambda: NOW)

    await _run(n8n, rec, store, enabled, rounds=2)

    assert len(rec.notes) == 2
    by_id = {n["metadata"]["execution_id"]: n for n in rec.notes}
    note = by_id["101"]
    assert note["tenant_id"] == T
    assert note["subject"] == "n8n: Invoice sync failed"
    assert note["metadata"] == {
        "source": "n8n",
        "workflow_id": "wf1",
        "execution_id": "101",
        "url": f"{BASE}/workflow/wf1/executions/101",
    }
    assert "node exploded in 101" in note["body"]
    assert f"{BASE}/workflow/wf1/executions/101" in note["body"]
    assert by_id["102"]["subject"] == "n8n: Lead router failed"
    assert await store.pending_alerts(T) == []
    assert rec.sleeps == [120]

    state = await store.get_sync_state(T)
    assert state is not None and state.last_ok_at == NOW and state.last_error is None
    assert (state.workflows_total, state.workflows_active) == (2, 1)
    rows = {r["workflow_id"]: r for r in await store.list_workflows(T, now=NOW)}
    assert rows["wf1"]["failures_24h"] == 1
    assert rows["wf1"]["last_error"] == "node exploded in 101"


async def test_a_new_failure_in_a_later_round_is_notified_alone(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    store = MemoryN8nStore(now=lambda: NOW)
    n8n.add_failure("101")
    await _run(n8n, rec, store, enabled, rounds=1)
    n8n.add_failure("103")
    await _run(n8n, rec, store, enabled, rounds=1)
    assert [n["metadata"]["execution_id"] for n in rec.notes] == ["101", "103"]


async def test_more_than_five_new_failures_coalesce_into_one_notification(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    for i in range(7):
        n8n.add_failure(str(200 + i), "wf1" if i % 2 else "wf2")
    store = MemoryN8nStore(now=lambda: NOW)

    await _run(n8n, rec, store, enabled, rounds=2)

    assert len(rec.notes) == 1
    note = rec.notes[0]
    assert note["subject"] == "n8n: 7 workflow executions failed"
    assert note["metadata"]["source"] == "n8n"
    assert note["metadata"]["count"] == 7
    assert sorted(note["metadata"]["execution_ids"]) == [str(200 + i) for i in range(7)]
    assert "Invoice sync" in note["body"] and "Lead router" in note["body"]
    assert await store.pending_alerts(T) == []


async def test_exactly_five_new_failures_are_notified_one_by_one(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    for i in range(5):
        n8n.add_failure(str(300 + i))
    await _run(n8n, rec, MemoryN8nStore(now=lambda: NOW), enabled, rounds=1)
    assert len(rec.notes) == 5


async def test_a_failed_notification_is_retried_next_round(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    n8n.add_failure("101")
    store = MemoryN8nStore(now=lambda: NOW)
    calls = {"n": 0}

    async def flaky(tenant_id, subject, body, metadata):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crm down")
        await rec.notify(tenant_id, subject, body, metadata)

    await poller.run(
        T,
        settings=enabled,
        client_factory=n8n.factory,
        store=store,
        notify=flaky,
        alert=rec.alert,
        sleep=rec.sleep,
        rounds=2,
        is_leader=lambda: True,
        now=lambda: NOW,
    )
    assert [n["metadata"]["execution_id"] for n in rec.notes] == ["101"]


async def test_a_failure_older_than_the_retention_window_is_ignored(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    n8n.add_failure("1", hours_ago=24 * 31)
    store = MemoryN8nStore(now=lambda: NOW)
    await _run(n8n, rec, store, enabled, rounds=1)
    assert rec.notes == []
    assert await store.list_executions(T, None) == []


async def test_a_follower_makes_no_http_call(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    n8n.add_failure("101")
    built = []

    def factory(settings):
        built.append(settings)
        return n8n.factory(settings)

    await poller.run(
        T,
        settings=enabled,
        client_factory=factory,
        store=MemoryN8nStore(now=lambda: NOW),
        notify=rec.notify,
        alert=rec.alert,
        sleep=rec.sleep,
        rounds=3,
        is_leader=lambda: False,
        now=lambda: NOW,
    )
    assert n8n.requests == [] and built == [] and rec.notes == []
    assert rec.sleeps == [120, 120]


async def test_three_failed_rounds_alert_once_and_recovery_resets(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    store = MemoryN8nStore(now=lambda: NOW)
    n8n.fail_with = 401

    await _run(n8n, rec, store, enabled, rounds=2)
    assert rec.alerts == []
    state = await store.get_sync_state(T)
    assert state is not None and state.last_error is not None and "auth" in state.last_error
    assert state.last_ok_at is None

    # One run keeps its outage count across rounds; a fresh run starts over.
    await _run(n8n, rec, store, enabled, rounds=5)
    assert len(rec.alerts) == 1
    level, title, body = rec.alerts[0]
    assert level == "warning"
    assert "n8n" in title
    assert "auth" in body
    assert KEY not in title + body

    # Recovery, then a second outage: one more alert, not a fresh one per round.
    calls = {"n": 0}

    def scripted(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # round 1 healthy (three requests), then broken
        n8n.fail_with = None if calls["n"] <= 3 else httpx.ConnectError("refused", request=request)
        return n8n.handler(request)

    def factory(_settings) -> N8nClient:
        return N8nClient(BASE, KEY, transport=httpx.MockTransport(scripted))

    rec.alerts.clear()
    n8n.fail_with = None
    await poller.run(
        T,
        settings=enabled,
        client_factory=factory,
        store=store,
        notify=rec.notify,
        alert=rec.alert,
        sleep=rec.sleep,
        rounds=8,
        is_leader=lambda: True,
        now=lambda: NOW,
    )
    assert len(rec.alerts) == 1
    assert "network" in rec.alerts[0][2]


async def test_an_outage_alert_resets_after_recovery_within_one_run(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    plan = iter([True, True, True, False, True, True, True, True])

    def factory(_settings) -> N8nClient:
        n8n.fail_with = 503 if next(plan) else None
        return N8nClient(BASE, KEY, transport=httpx.MockTransport(n8n.handler))

    await poller.run(
        T,
        settings=enabled,
        client_factory=factory,
        store=MemoryN8nStore(now=lambda: NOW),
        notify=rec.notify,
        alert=rec.alert,
        sleep=rec.sleep,
        rounds=8,
        is_leader=lambda: True,
        now=lambda: NOW,
    )
    assert len(rec.alerts) == 2


async def test_a_missing_key_fails_the_round_without_any_request(configure) -> None:
    settings = configure(ROBOTHOR_N8N_ENABLED="true", ROBOTHOR_N8N_BASE_URL=BASE)
    store = MemoryN8nStore(now=lambda: NOW)
    rec = Recorder()
    await poller.run(
        T,
        settings=settings,
        store=store,
        notify=rec.notify,
        alert=rec.alert,
        sleep=rec.sleep,
        rounds=1,
        is_leader=lambda: True,
        now=lambda: NOW,
        secret=lambda _name: None,
    )
    state = await store.get_sync_state(T)
    assert state is not None and state.last_error is not None
    assert "not_configured" in state.last_error


async def test_the_default_client_factory_reads_the_key_through_the_vault_accessor(
    enabled, monkeypatch
) -> None:
    looked_up: list[str] = []

    def fake_secret(name: str) -> str | None:
        looked_up.append(name)
        return KEY

    client = poller.client_from_settings(enabled, secret=fake_secret)
    assert looked_up == ["ROBOTHOR_N8N_API_KEY"]
    assert KEY not in repr(client)
    await client.aclose()


async def test_purge_runs_with_the_round(enabled) -> None:
    n8n, rec = FakeN8n(), Recorder()
    store = MemoryN8nStore(now=lambda: NOW)
    await store.record_executions(
        T,
        [
            {
                "execution_id": "old",
                "workflow_id": "wf1",
                "status": "error",
                "mode": "trigger",
                "started_at": NOW - timedelta(days=40),
                "stopped_at": None,
            }
        ],
    )
    await store.mark_alerted(T, ["old"])
    await _run(n8n, rec, store, enabled, rounds=1)
    assert await store.list_executions(T, None) == []


async def test_the_poll_interval_has_a_floor(configure) -> None:
    settings = configure(
        ROBOTHOR_N8N_ENABLED="true",
        ROBOTHOR_N8N_BASE_URL=BASE,
        ROBOTHOR_N8N_POLL_SECONDS="1",
    )
    n8n, rec = FakeN8n(), Recorder()
    await _run(n8n, rec, MemoryN8nStore(now=lambda: NOW), settings, rounds=2)
    assert rec.sleeps == [poller.MIN_INTERVAL_SECONDS]


async def test_the_daemon_starts_the_monitor_only_when_enabled(configure, monkeypatch) -> None:
    from robothor.engine import daemon

    started: list[str] = []

    async def fake_run(tenant_id, **_kw):
        started.append(tenant_id)

    async def idle(_tenant):
        return None

    monkeypatch.setattr(poller, "run", fake_run)
    monkeypatch.setattr("robothor.engine.calendar_recovery_worker.run", idle)
    monkeypatch.setattr("robothor.workspace.ingest.worker.start_tasks", lambda _t: [])

    configure()
    tasks = daemon._background_workers(T)
    assert "n8n-monitor" not in {t.get_name() for t in tasks}
    for task in tasks:
        await task

    configure(ROBOTHOR_N8N_ENABLED="true", ROBOTHOR_N8N_BASE_URL=BASE)
    tasks = daemon._background_workers(T)
    assert "n8n-monitor" in {t.get_name() for t in tasks}
    for task in tasks:
        await task
    assert started == [T]
