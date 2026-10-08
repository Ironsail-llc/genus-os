"""The ingest worker: dark on Google, rounds on Microsoft 365."""

from __future__ import annotations

import asyncio

import pytest

from robothor.events import contract
from robothor.workspace.ingest import worker
from robothor.workspace.ingest.email_log import EmailLog
from robothor.workspace.ingest.state import MemoryIngestStore
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_ingest import install_ingest
from robothor.workspace.tests.fake_graph_mail import install_mail

ME = "assistant@example.com"
OWNER = "owner@example.com"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


@pytest.fixture
def configure(monkeypatch):
    from robothor.settings import reset_settings

    def _set(**pairs: str) -> None:
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    for name in (
        "ROBOTHOR_WORKSPACE_PROVIDER",
        "ROBOTHOR_M365_ASSISTANT_MAILBOX",
        "ROBOTHOR_M365_OWNER_MAILBOX",
        "ROBOTHOR_M365_INGEST_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield _set
    reset_settings()


def test_the_worker_is_dark_on_google(configure) -> None:
    configure(ROBOTHOR_WORKSPACE_PROVIDER="google")
    assert worker.ingest_enabled() is False


async def test_google_starts_no_task_and_run_touches_nothing(configure) -> None:
    configure()  # the default provider is google
    assert worker.start_tasks("tenant-a") == []

    async def no_graph(_tenant):
        raise AssertionError("google must never build a Graph client")

    async def no_publish(*_a):
        raise AssertionError("google must never publish from the platform ingest")

    store = MemoryIngestStore()
    await worker.run("tenant-a", graph_factory=no_graph, publish=no_publish, store=store, rounds=3)
    assert store.states == {} and store.seen_at == {}


async def test_microsoft365_starts_the_ingest_task(configure, monkeypatch) -> None:
    configure(ROBOTHOR_WORKSPACE_PROVIDER="microsoft365", ROBOTHOR_M365_ASSISTANT_MAILBOX=ME)
    started = asyncio.Event()

    async def fake_run(tenant_id):
        started.set()

    monkeypatch.setattr(worker, "run", fake_run)
    tasks = worker.start_tasks("tenant-a")
    assert [t.get_name() for t in tasks] == ["m365-ingest"]
    await asyncio.gather(*tasks)
    assert started.is_set()


async def test_rounds_publish_mail_and_calendar_then_sleep_the_interval(
    configure, tmp_path
) -> None:
    configure(
        ROBOTHOR_WORKSPACE_PROVIDER="microsoft365",
        ROBOTHOR_M365_ASSISTANT_MAILBOX=ME,
        ROBOTHOR_M365_OWNER_MAILBOX=OWNER,
        ROBOTHOR_M365_INGEST_INTERVAL_SECONDS="45",
    )
    tenant = FakeGraphTenant()
    delta = install_ingest(tenant, install_mail(tenant))
    message = delta.deliver(ME, sender="alice@example.com", subject="Hi")
    events: list[tuple[str, str, dict]] = []
    slept: list[float] = []
    closed: list[bool] = []

    async def graph_factory(_tenant):
        client = GraphClient(StaticToken(tenant), transport=tenant.transport())
        real_close = client.aclose

        async def aclose():
            closed.append(True)
            await real_close()

        client.aclose = aclose  # type: ignore[method-assign]
        return client

    async def publish(stream, event_type, payload):
        events.append((stream, event_type, payload))

    async def sleep(seconds):
        slept.append(seconds)
        start, end = "2026-10-06T15:00:00Z", "2026-10-06T15:30:00Z"
        if len(slept) == 1:
            delta.add_event(OWNER, subject="Sync", start=start, end=end)

    store = MemoryIngestStore(now=lambda: delta.mail.clock)
    await worker.run(
        "tenant-a",
        graph_factory=graph_factory,
        store=store,
        publish=publish,
        email_log=EmailLog(tmp_path / "email-log.json"),
        sleep=sleep,
        rounds=3,
        now=lambda: delta.mail.clock,
        leader=lambda: True,
    )

    assert [(s, t) for s, t, _p in events] == [
        (contract.EMAIL_STREAM, contract.EMAIL_NEW),
        (contract.CALENDAR_STREAM, contract.CALENDAR_NEW),
    ]
    assert events[0][2]["id"] == message["id"]
    assert slept == [45, 45]
    assert closed == [True]
    assert {key[3] for key in store.states} == {"mail", "calendar"}


async def test_a_follower_replica_does_not_ingest(configure) -> None:
    configure(ROBOTHOR_WORKSPACE_PROVIDER="microsoft365", ROBOTHOR_M365_ASSISTANT_MAILBOX=ME)

    async def no_graph(_tenant):
        raise AssertionError("only the leader builds a Graph client")

    async def sleep(_s):
        return None

    store = MemoryIngestStore()
    await worker.run(
        "tenant-a", graph_factory=no_graph, store=store, sleep=sleep, rounds=2, leader=lambda: False
    )
    assert store.states == {}


async def test_a_missing_credential_is_retried_not_fatal(configure) -> None:
    configure(ROBOTHOR_WORKSPACE_PROVIDER="microsoft365", ROBOTHOR_M365_ASSISTANT_MAILBOX=ME)
    from robothor.workspace.errors import AuthError

    attempts: list[str] = []

    async def graph_factory(tenant_id):
        attempts.append(tenant_id)
        raise AuthError("microsoft365 not connected")

    async def sleep(_s):
        return None

    async def publish(*_a):
        raise AssertionError("nothing to publish without a credential")

    await worker.run(
        "tenant-a",
        graph_factory=graph_factory,
        store=MemoryIngestStore(),
        publish=publish,
        sleep=sleep,
        rounds=2,
        leader=lambda: True,
    )
    assert attempts == ["tenant-a", "tenant-a"]


async def test_one_failing_ingestor_does_not_stop_the_other(caplog) -> None:
    from robothor.workspace.ingest.base import Ingestor, IngestReport

    ran: list[str] = []

    class Broken(Ingestor):
        resource = "mail"

        async def run_once(self):
            raise RuntimeError("boom")

    class Fine(Ingestor):
        resource = "calendar"

        async def run_once(self):
            ran.append("calendar")
            return IngestReport(resource="calendar")

    await worker.run_round([Broken(), Fine()])
    assert ran == ["calendar"]
    assert "mail ingest deferred: RuntimeError" in caplog.text


def test_the_daemon_starts_the_ingest_through_start_tasks() -> None:
    """The daemon wires the worker in through start_tasks (dark on google), not run()."""
    from pathlib import Path

    source = (Path(worker.__file__).resolve().parents[2] / "engine" / "daemon.py").read_text()
    assert "ingest_worker.start_tasks(tenant_id)" in source
    assert "*_background_workers(config.tenant_id)," in source
