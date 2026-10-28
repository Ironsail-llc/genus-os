"""Gmail push: a push wakes one coalesced ingest round; the watch is renewed daily."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from robothor.workspace.ingest import gmail_push, worker
from robothor.workspace.ingest.state import MemoryIngestStore
from robothor.workspace.tests.fake_gmail_history import ME, FakeGmailHistory

TOPIC = "projects/example-project/topics/gmail-push"


# ── coalescing ─────────────────────────────────────────────────────


async def test_a_burst_runs_one_round_in_flight_and_one_pending() -> None:
    gate = asyncio.Event()
    started = 0

    async def round_() -> None:
        nonlocal started
        started += 1
        await gate.wait()

    coalescer = gmail_push.RoundCoalescer(round_)
    server = asyncio.create_task(coalescer.serve())
    coalescer.trigger()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == 1
    for _ in range(10):
        coalescer.trigger()  # all arrive while the first round is in flight
    gate.set()
    await coalescer.settle()
    assert started == 2
    server.cancel()


async def test_a_failing_round_does_not_stop_the_next() -> None:
    calls = 0

    async def round_() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")

    coalescer = gmail_push.RoundCoalescer(round_)
    server = asyncio.create_task(coalescer.serve())
    coalescer.trigger()
    await coalescer.settle()
    coalescer.trigger()
    await coalescer.settle()
    assert calls == 2
    assert not server.done()
    server.cancel()


# ── which pushes count ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"emailAddress": ME, "historyId": 5}, True),
        ({"emailAddress": ME.upper(), "historyId": 5}, True),
        ({"emailAddress": "bob@example.com", "historyId": 5}, False),
        ({"historyId": 5}, False),
        ({}, False),
    ],
)
def test_push_matches_the_configured_mailbox(payload: dict[str, Any], expected: bool) -> None:
    assert gmail_push.push_matches(payload, ME) is expected


# ── the Redis source ───────────────────────────────────────────────


class FakeStreamRedis:
    def __init__(self, entries: list[tuple[bytes, dict[bytes, bytes]]]) -> None:
        self.entries = entries
        self.groups: list[tuple[str, str]] = []
        self.acked: list[Any] = []

    async def xgroup_create(self, key: str, group: str, id: str = "$", mkstream: bool = False):  # noqa: A002
        self.groups.append((key, group))

    async def xreadgroup(self, group, consumer, streams, count=None, block=None):
        if not self.entries:
            return []
        out, self.entries = self.entries, []
        return [(next(iter(streams)).encode(), out)]

    async def xack(self, key: str, group: str, *ids: Any) -> int:
        self.acked.extend(ids)
        return len(ids)


def _envelope(payload: Any) -> dict[bytes, bytes]:
    return {
        b"type": b"gmail.push",
        b"payload": payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
    }


async def test_redis_source_reads_payloads_and_acks() -> None:
    redis = FakeStreamRedis(
        [
            (b"1-0", _envelope({"emailAddress": ME, "historyId": 7})),
            (b"2-0", _envelope(b"not json")),
        ]
    )
    source = gmail_push.RedisPushSource(redis, stream="gmail")
    payloads = await source.read()
    assert payloads == [{"emailAddress": ME, "historyId": 7}]
    assert redis.groups == [("robothor:events:gmail", gmail_push.CONSUMER_GROUP)]
    assert redis.acked == [b"1-0", b"2-0"]
    assert await source.read() == []


# ── watch renewal ──────────────────────────────────────────────────


async def test_renew_watch_asks_for_the_inbox_on_the_topic() -> None:
    gmail = FakeGmailHistory()
    gmail.watch_expiration = "1767225600000"
    expires = await gmail_push.renew_watch(gmail, TOPIC)
    assert gmail.watches == [(TOPIC, ["INBOX"])]
    assert expires == datetime(2026, 1, 1, tzinfo=UTC)


# ── the loop ───────────────────────────────────────────────────────


class ListSource:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = batches
        self.reads = 0

    async def read(self) -> list[dict[str, Any]]:
        self.reads += 1
        return self.batches.pop(0) if self.batches else []


class Recorder:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def __call__(self, stream: str, event_type: str, payload: dict[str, Any]) -> None:
        self.ids.append(payload["id"])


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


async def test_run_watches_at_start_and_ingests_on_a_matching_push() -> None:
    gmail = FakeGmailHistory(history_id=100)
    publish = Recorder()
    source = ListSource([[], [{"emailAddress": "bob@example.com", "historyId": 1}], []])

    async def deliver_then_push() -> list[dict[str, Any]]:
        gmail.deliver("m1")
        return [{"emailAddress": ME, "historyId": gmail.history_id}]

    batches: list[Any] = [source.read, source.read, deliver_then_push, source.read]

    class Scripted:
        async def read(self) -> list[dict[str, Any]]:
            return await batches.pop(0)() if batches else []

    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic=TOPIC,
        gmail=gmail,
        store=MemoryIngestStore(),
        publish=publish,
        source=Scripted(),
        leader=lambda: True,
        sleep=_no_sleep,
        rounds=4,
    )
    assert gmail.watches == [(TOPIC, ["INBOX"])]
    assert publish.ids == ["m1"]


async def test_run_renews_the_watch_daily() -> None:
    gmail = FakeGmailHistory(history_id=100)
    now = [datetime(2026, 1, 1, tzinfo=UTC)]

    class Ticking:
        async def read(self) -> list[dict[str, Any]]:
            now[0] += timedelta(hours=13)
            return []

    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic=TOPIC,
        gmail=gmail,
        store=MemoryIngestStore(),
        publish=Recorder(),
        source=Ticking(),
        leader=lambda: True,
        sleep=_no_sleep,
        clock=lambda: now[0],
        rounds=4,
    )
    # At start, then once a day: 52 hours of reads -> two renewals after the first.
    assert len(gmail.watches) == 3


async def test_run_without_a_topic_never_watches_but_still_ingests() -> None:
    gmail = FakeGmailHistory(history_id=100)
    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic="",
        gmail=gmail,
        store=MemoryIngestStore(),
        publish=Recorder(),
        source=ListSource([]),
        leader=lambda: True,
        sleep=_no_sleep,
        rounds=1,
    )
    assert gmail.watches == []
    assert gmail.history_calls == []  # the first round only set the cursor


async def test_a_follower_neither_watches_nor_reads() -> None:
    gmail = FakeGmailHistory(history_id=100)
    source = ListSource([[{"emailAddress": ME, "historyId": 1}]])
    store = MemoryIngestStore()
    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic=TOPIC,
        gmail=gmail,
        store=store,
        publish=Recorder(),
        source=source,
        leader=lambda: False,
        sleep=_no_sleep,
        rounds=3,
    )
    assert gmail.watches == []
    assert source.reads == 0
    assert store.states == {}


# ── the worker starts it on google, only when enabled ─────────────


@pytest.fixture
def configure(monkeypatch):
    from robothor.settings import reset_settings

    def _set(**pairs: str) -> None:
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    for name in (
        "ROBOTHOR_WORKSPACE_PROVIDER",
        "ROBOTHOR_GMAIL_PUSH_ENABLED",
        "ROBOTHOR_GMAIL_PUSH_MAILBOX",
        "ROBOTHOR_GMAIL_PUSH_TOPIC",
        "ROBOTHOR_GMAIL_PUSH_STREAM",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield _set
    reset_settings()


def test_settings_defaults(configure) -> None:
    from robothor.settings import get_settings

    configure()
    ws = get_settings().workspace
    assert ws.gmail_push_enabled is False
    assert ws.gmail_push_mailbox == ""
    assert ws.gmail_push_topic == ""
    assert ws.gmail_push_stream == "gmail"


async def test_google_push_is_off_by_default(configure) -> None:
    configure()
    assert worker.start_tasks("test-tenant") == []


async def test_google_push_starts_one_task_when_enabled(configure, monkeypatch) -> None:
    configure(ROBOTHOR_GMAIL_PUSH_ENABLED="true", ROBOTHOR_GMAIL_PUSH_MAILBOX=ME)
    seen: dict[str, Any] = {}

    async def fake_run(tenant_id: str, **kwargs: Any) -> None:
        seen["tenant"] = tenant_id

    monkeypatch.setattr(gmail_push, "run_from_settings", fake_run)
    tasks = worker.start_tasks("test-tenant")
    assert [t.get_name() for t in tasks] == ["gmail-push"]
    await asyncio.gather(*tasks)
    assert seen["tenant"] == "test-tenant"


async def test_push_settings_do_nothing_on_microsoft365(configure, monkeypatch) -> None:
    configure(ROBOTHOR_WORKSPACE_PROVIDER="microsoft365", ROBOTHOR_GMAIL_PUSH_ENABLED="true")

    async def fake_m365(tenant_id: str, **_k: Any) -> None:
        return None

    monkeypatch.setattr(worker, "run", fake_m365)
    tasks = worker.start_tasks("test-tenant")
    assert [t.get_name() for t in tasks] == ["m365-ingest"]
    await asyncio.gather(*tasks)


# ── the seen set is purged, as on Microsoft 365 ───────────────────


class CountingStore(MemoryIngestStore):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.purges: list[datetime] = []

    async def purge_seen(self, tenant_id: str, older_than: datetime) -> int:
        self.purges.append(older_than)
        return await super().purge_seen(tenant_id, older_than)


async def test_run_purges_old_seen_ids_hourly() -> None:
    from robothor.workspace.ingest.state import SEEN_TTL
    from robothor.workspace.ingest.worker import PURGE_EVERY_SECONDS

    start = datetime(2026, 3, 1, tzinfo=UTC)
    now = [start]
    store = CountingStore(now=lambda: now[0])
    now[0] = start - timedelta(days=50)
    await store.mark_seen("test-tenant", "google", ["mail:ancient"])
    now[0] = start - timedelta(days=1)
    await store.mark_seen("test-tenant", "google", ["mail:recent"])
    now[0] = start

    class Ticking:
        async def read(self) -> list[dict[str, Any]]:
            now[0] += timedelta(seconds=PURGE_EVERY_SECONDS / 2 + 1)
            return []

    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic="",
        gmail=FakeGmailHistory(history_id=100),
        store=store,
        publish=Recorder(),
        source=Ticking(),
        leader=lambda: True,
        sleep=_no_sleep,
        clock=lambda: now[0],
        rounds=4,
    )
    # At start, then once per PURGE_EVERY_SECONDS: four half-interval reads -> two more.
    assert len(store.purges) == 3
    assert store.purges[0] == start - SEEN_TTL
    remaining = {key[2] for key in store.seen_at}
    assert "mail:ancient" not in remaining
    assert "mail:recent" in remaining


async def test_a_failing_purge_does_not_stop_the_loop() -> None:
    class Broken(MemoryIngestStore):
        async def purge_seen(self, tenant_id: str, older_than: datetime) -> int:
            raise RuntimeError("db away")

    source = ListSource([])
    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic="",
        gmail=FakeGmailHistory(history_id=100),
        store=Broken(),
        publish=Recorder(),
        source=source,
        leader=lambda: True,
        sleep=_no_sleep,
        rounds=2,
    )
    assert source.reads == 2


async def test_a_follower_does_not_purge() -> None:
    store = CountingStore()
    await gmail_push.run(
        "test-tenant",
        mailbox=ME,
        topic="",
        gmail=FakeGmailHistory(history_id=100),
        store=store,
        publish=Recorder(),
        source=ListSource([]),
        leader=lambda: False,
        sleep=_no_sleep,
        rounds=2,
    )
    assert store.purges == []
