"""Microsoft 365 ingestion against a fake Exchange tenant with delta queries.

The property everything here protects: an agent answers each new message
ONCE, and never answers old mail again because Exchange threw a delta away.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from robothor.events import contract
from robothor.workspace.ingest.email_log import EmailLog
from robothor.workspace.ingest.microsoft import (
    INITIAL_PUBLISH_LIMIT,
    GraphCalendarIngestor,
    GraphMailIngestor,
)
from robothor.workspace.ingest.state import MemoryIngestStore
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_ingest import FakeDelta, install_ingest
from robothor.workspace.tests.fake_graph_mail import install_mail

ME = "assistant@example.com"
OWNER = "owner@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"
TENANT = "tenant-a"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


class Bus:
    """Records every event an ingestor publishes, validating it against the contract."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def __call__(self, stream: str, event_type: str, payload: dict) -> None:
        if event_type == contract.EMAIL_NEW:
            contract.validate("email_new", payload)
        else:
            assert stream == contract.CALENDAR_STREAM
            contract.validate("calendar_event", payload)
        self.events.append((stream, event_type, payload))

    def ids(self, event_type: str = contract.EMAIL_NEW) -> list[str]:
        return [p["id"] for _s, t, p in self.events if t == event_type]

    def types(self) -> list[tuple[str, str]]:
        return [(t, p["id"]) for _s, t, p in self.events]


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def delta(tenant: FakeGraphTenant) -> FakeDelta:
    return install_ingest(tenant, install_mail(tenant))


@pytest.fixture
async def graph(tenant: FakeGraphTenant):
    client = GraphClient(StaticToken(tenant), transport=tenant.transport())
    yield client
    await client.aclose()


@pytest.fixture
def store(delta: FakeDelta) -> MemoryIngestStore:
    return MemoryIngestStore(now=lambda: delta.mail.clock)


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def log(tmp_path) -> EmailLog:
    return EmailLog(tmp_path / "memory" / "email-log.json")


@pytest.fixture
def mail(graph, store, bus, log, delta) -> GraphMailIngestor:
    return GraphMailIngestor(
        graph=graph,
        mailbox=ME,
        tenant_id=TENANT,
        store=store,
        publish=bus,
        email_log=log,
        now=lambda: delta.mail.clock,
    )


@pytest.fixture
def calendar(graph, store, bus, delta) -> GraphCalendarIngestor:
    return GraphCalendarIngestor(
        graph=graph,
        mailbox=OWNER,
        tenant_id=TENANT,
        store=store,
        publish=bus,
        now=lambda: delta.mail.clock,
    )


def _deliver(delta: FakeDelta, n: int = 1, **kwargs) -> list[dict]:
    return [
        delta.deliver(ME, sender=kwargs.pop("sender", ALICE), subject=f"Note {i}", **kwargs)
        for i in range(n)
    ]


# ── mail: initial and incremental ──────────────────────────────────────


async def test_initial_sync_publishes_each_unread_message_once(mail, delta, bus, log) -> None:
    unread = _deliver(delta, 3)
    read = delta.deliver(ME, sender=BOB, subject="Already read", is_read=True)
    delta.deliver(ME, sender=BOB, subject="Sent", folder="sentitems")

    report = await mail.run_once()

    assert report.mode == "initial"
    assert bus.ids() == [m["id"] for m in unread]
    assert read["id"] not in bus.ids()
    # A second round with nothing new publishes nothing.
    report = await mail.run_once()
    assert report.mode == "incremental"
    assert bus.ids() == [m["id"] for m in unread]


async def test_the_payload_uses_the_mail_tools_translation(mail, delta, bus) -> None:
    sent = delta.deliver(ME, sender=ALICE, sender_name="Alice Example", subject="Lunch?")
    await mail.run_once()
    [(_stream, event_type, payload)] = bus.events
    assert event_type == "email.new"
    assert payload["id"] == sent["id"]
    assert payload["from"] == "Alice Example <alice@example.com>"
    assert payload["subject"] == "Lunch?"
    assert "UNREAD" in payload["labels"] and "INBOX" in payload["labels"]
    assert payload["threadId"] == sent["conversationId"]
    assert payload["provider"] == "microsoft365"
    assert payload["date"].endswith("+0000")  # RFC 2822, as the Date header


async def test_the_delta_request_is_paged_and_selects_envelope_fields(
    mail, delta, bus, tenant
) -> None:
    _deliver(delta, 3)
    await mail.run_once()
    first = delta.delta_requests("mail")[0]
    assert "odata.maxpagesize=50" in first.headers["prefer"]
    assert 'IdType="ImmutableId"' in first.headers["prefer"]
    assert "receivedDateTime ge" in first.url.params["$filter"]
    assert "body" not in first.url.params["$select"].split(",")


async def test_a_multi_page_initial_sync_follows_every_page(mail, delta, bus, monkeypatch) -> None:
    from robothor.workspace.ingest import microsoft

    monkeypatch.setattr(microsoft, "_PREFER_PAGE", "odata.maxpagesize=2")
    unread = _deliver(delta, 5)
    await mail.run_once()
    assert bus.ids() == [m["id"] for m in unread]
    assert len(delta.delta_requests("mail")) == 3


async def test_initial_sync_publishes_at_most_the_limit_newest_first(mail, delta, bus) -> None:
    many = _deliver(delta, INITIAL_PUBLISH_LIMIT + 5)
    await mail.run_once()
    assert bus.ids() == [m["id"] for m in many[-INITIAL_PUBLISH_LIMIT:]]
    await mail.run_once()
    assert len(bus.ids()) == INITIAL_PUBLISH_LIMIT


async def test_incremental_delta_publishes_only_new_messages(mail, delta, bus) -> None:
    first = _deliver(delta, 2)
    await mail.run_once()
    delta.mark_read(ME, first[0]["id"])  # a change to an old message
    later = _deliver(delta, 2)
    report = await mail.run_once()
    assert report.mode == "incremental"
    assert bus.ids() == [m["id"] for m in first + later]
    # The incremental round used the deltaLink, not a fresh query.
    assert "$deltatoken" in delta.delta_requests("mail")[-1].url.params


async def test_a_new_message_that_is_already_read_is_not_published(mail, delta, bus) -> None:
    await mail.run_once()
    delta.deliver(ME, sender=ALICE, is_read=True)
    await mail.run_once()
    assert bus.ids() == []


# ── mail: the 410 resync never replays ─────────────────────────────────


async def test_a_410_resync_publishes_no_duplicate_and_no_old_mail(mail, delta, bus, store) -> None:
    old = _deliver(delta, 3)
    old_read = delta.deliver(ME, sender=BOB, is_read=True)
    await mail.run_once()
    high_water = (await store.load(TENANT, "microsoft365", ME, "mail")).high_water
    assert high_water is not None

    # Exchange forgets the delta; meanwhile one old message is marked unread
    # again (it now looks "unread" in the fresh full listing) and new mail arrives.
    delta.expire_tokens()
    delta.mail.message(ME, old_read["id"])["isRead"] = False
    new = _deliver(delta, 2)

    report = await mail.run_once()

    assert report.mode == "resync"
    published = bus.ids()
    assert len(published) == len(set(published)), "an email.new was published twice"
    assert published == [m["id"] for m in old + new]
    assert old_read["id"] not in published, "a message older than the high-water mark replayed"
    assert report.held_back == 0  # the old ones were already seen
    # The fresh delta's position was saved: the next round is incremental again.
    assert (await mail.run_once()).mode == "incremental"
    assert bus.ids() == published


async def test_a_resync_after_the_seen_set_is_lost_still_holds_old_mail_back(
    mail, delta, bus, store
) -> None:
    """Even with no seen ids at all, nothing at or below the high-water mark is replayed."""
    old = _deliver(delta, 3)
    await mail.run_once()
    store.seen_at.clear()
    delta.expire_tokens()
    new = delta.deliver(ME, sender=ALICE, subject="After the reset")

    report = await mail.run_once()

    assert report.mode == "resync"
    assert bus.ids() == [m["id"] for m in old] + [new["id"]]
    assert report.held_back == len(old)


async def test_a_lost_state_row_with_seen_ids_is_not_a_replay(mail, delta, bus, store) -> None:
    old = _deliver(delta, 2)
    await mail.run_once()
    store.states.clear()  # the position is gone; the seen set survived
    await mail.run_once()
    assert bus.ids() == [m["id"] for m in old]


async def test_a_failed_publish_is_retried_next_round_without_duplicates(mail, delta, bus) -> None:
    from robothor.workspace.ingest.base import PublishFailed

    msgs = _deliver(delta, 3)
    calls = {"n": 0}
    real = mail.publish

    async def flaky(stream, event_type, payload):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PublishFailed("bus down")
        await real(stream, event_type, payload)

    mail.publish = flaky
    with pytest.raises(PublishFailed):
        await mail.run_once()
    mail.publish = real
    await mail.run_once()
    assert bus.ids() == [m["id"] for m in msgs]


# ── email-log.json ─────────────────────────────────────────────────────


async def test_new_mail_is_merged_into_the_email_log(mail, delta, bus, log) -> None:
    log.path.parent.mkdir(parents=True)
    existing = {
        "lastCheckedAt": None,
        "entries": {"older-id": {"id": "older-id", "category": "triaged", "custom": 1}},
    }
    log.path.write_text(json.dumps(existing))
    sent = delta.deliver(ME, sender=ALICE, subject="Logged")
    await mail.run_once()

    data = json.loads(log.path.read_text())
    contract.validate("email_log", {**data, "entries": {sent["id"]: data["entries"][sent["id"]]}})
    assert data["entries"]["older-id"] == existing["entries"]["older-id"]
    entry = data["entries"][sent["id"]]
    assert entry["subject"] == "Logged" and entry["categorizedAt"] is None
    assert entry["provider"] == "microsoft365"
    assert data["lastCheckedAt"]
    # The event and the log agree.
    [(_s, _t, payload)] = bus.events
    assert contract.email_new_payload(entry) == payload


# ── calendar ───────────────────────────────────────────────────────────


def _slot(delta: FakeDelta, hours: int, minutes: int = 30) -> tuple[str, str]:
    start = delta.mail.clock + timedelta(hours=hours)
    end = start + timedelta(minutes=minutes)
    return start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), end.strftime("%Y-%m-%dT%H:%M:%S.0000000")


async def test_the_first_calendar_sync_is_a_baseline(calendar, delta, bus) -> None:
    start, end = _slot(delta, 2)
    delta.add_event(OWNER, subject="Standing", start=start, end=end, attendees=(ALICE,))
    report = await calendar.run_once()
    assert report.mode == "initial"
    assert bus.events == []
    request = delta.delta_requests("calendar")[0]
    assert request.url.params["startDateTime"] and request.url.params["endDateTime"]


async def test_calendar_delta_emits_created_updated_rescheduled_cancelled(
    calendar, delta, bus
) -> None:
    await calendar.run_once()
    start, end = _slot(delta, 3)
    event = delta.add_event(
        OWNER,
        subject="Kickoff",
        start=start,
        end=end,
        attendees=(ALICE, BOB),
        join_url="https://teams.example/j/1",
    )
    await calendar.run_once()
    delta.update_event(OWNER, event["id"], subject="Kickoff (agenda)")
    await calendar.run_once()
    new_start, new_end = _slot(delta, 5)
    delta.update_event(OWNER, event["id"], start=new_start, end=new_end)
    await calendar.run_once()
    delta.cancel_event(OWNER, event["id"])
    await calendar.run_once()
    await calendar.run_once()  # nothing changed: nothing published

    assert bus.types() == [
        ("calendar.new", event["id"]),
        ("calendar.modified", event["id"]),
        ("calendar.rescheduled", event["id"]),
        ("calendar.cancellation", event["id"]),
    ]
    created = bus.events[0][2]
    assert created["title"] == "Kickoff"
    assert created["attendees"] == [ALICE, BOB]
    assert created["hangoutLink"] == "https://teams.example/j/1"
    assert created["start"].endswith("Z") and created["provider"] == "microsoft365"


async def test_a_deleted_event_is_a_cancellation(calendar, delta, bus) -> None:
    start, end = _slot(delta, 2)
    event = delta.add_event(OWNER, subject="Gone", start=start, end=end)
    await calendar.run_once()
    delta.delete_event(OWNER, event["id"])
    await calendar.run_once()
    assert bus.types() == [("calendar.cancellation", event["id"])]


async def test_an_event_moved_out_of_the_window_is_rescheduled_not_cancelled(
    calendar, delta, bus
) -> None:
    start, end = _slot(delta, 2)
    event = delta.add_event(OWNER, subject="Later", start=start, end=end)
    await calendar.run_once()
    far_start, far_end = _slot(delta, 24 * 60)
    delta.update_event(OWNER, event["id"], start=far_start, end=far_end)
    await calendar.run_once()
    assert bus.types() == [("calendar.rescheduled", event["id"])]


async def test_a_calendar_410_or_daily_reinit_does_not_replay(calendar, delta, bus, store) -> None:
    start, end = _slot(delta, 2)
    standing = delta.add_event(OWNER, subject="Standing", start=start, end=end)
    await calendar.run_once()
    delta.expire_tokens()
    s2, e2 = _slot(delta, 4)
    fresh = delta.add_event(OWNER, subject="Fresh", start=s2, end=e2)
    report = await calendar.run_once()
    assert report.mode == "resync"
    assert bus.types() == [("calendar.new", fresh["id"])]

    # A day later the window is renewed with a new delta: still no replay.
    delta.mail.clock += timedelta(days=1, minutes=1)
    report = await calendar.run_once()
    assert report.mode == "resync"
    assert bus.types() == [("calendar.new", fresh["id"])]
    assert standing["id"] not in [i for _t, i in bus.types()]
