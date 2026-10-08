"""Microsoft 365 ingest -> CRM: each new email is logged as an interaction ONCE.

The Google instance's sync script posts every new email to the bridge's
``/log-interaction``. The Microsoft 365 ingestor does the same through
:func:`robothor.crm.interactions.log_interaction`, in process; here the CRM
boundary is a recording fake, so no database is needed.
"""

from __future__ import annotations

import json

import pytest

from robothor.workspace.ingest.base import PublishFailed
from robothor.workspace.ingest.crm import interaction_payload, parse_sender
from robothor.workspace.ingest.email_log import EmailLog
from robothor.workspace.ingest.microsoft import GraphMailIngestor
from robothor.workspace.ingest.state import MemoryIngestStore
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_ingest import FakeDelta, install_ingest
from robothor.workspace.tests.fake_graph_mail import install_mail

ME = "assistant@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"
TENANT = "tenant-a"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


class Crm:
    """Records every interaction the ingestor logs. ``ok=False`` = the CRM refused it."""

    def __init__(self) -> None:
        self.logged: list[dict] = []
        self.ok = True
        self.raises: Exception | None = None

    async def __call__(self, payload: dict) -> bool:
        if self.raises is not None:
            raise self.raises
        self.logged.append(payload)
        return self.ok


class Bus:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.fail_next = 0

    async def __call__(self, stream: str, event_type: str, payload: dict) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise PublishFailed("event bus did not accept email.new")
        self.ids.append(payload["id"])


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
def crm() -> Crm:
    return Crm()


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def log(tmp_path) -> EmailLog:
    return EmailLog(tmp_path / "memory" / "email-log.json")


@pytest.fixture
def mail(graph, store, bus, log, delta, crm) -> GraphMailIngestor:
    return GraphMailIngestor(
        graph=graph,
        mailbox=ME,
        tenant_id=TENANT,
        store=store,
        publish=bus,
        email_log=log,
        log_interaction=crm,
        now=lambda: delta.mail.clock,
    )


# ── the payload: what the Google script sends ──────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Alice Example" <alice@example.com>', ("Alice Example", ALICE)),
        ("Alice Example <alice@example.com>", ("Alice Example", ALICE)),
        ("<alice@example.com>", ("alice", ALICE)),
        ("alice@example.com", ("alice", ALICE)),
        ("Alice Example", ("Alice Example", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_sender_matches_the_google_script(raw, expected) -> None:
    assert parse_sender(raw) == expected


def test_the_payload_is_the_bridge_log_interaction_body() -> None:
    entry = {"from": "Alice Example <alice@example.com>", "subject": "Lunch?"}
    assert interaction_payload(entry) == {
        "contact_name": "Alice Example",
        "channel": "email",
        "direction": "incoming",
        "content_summary": "Alice Example <alice@example.com>: 'Lunch?'",
        "channel_identifier": ALICE,
    }
    no_subject = interaction_payload({"from": ALICE, "subject": None})
    assert no_subject is not None
    assert no_subject["content_summary"] == "alice@example.com: '(no subject)'"
    assert interaction_payload({"from": None, "subject": "x"}) is None


# ── the ingestor logs each new email once ──────────────────────────────


async def test_a_new_email_logs_exactly_one_interaction(mail, delta, crm, bus, log) -> None:
    sent = delta.deliver(ME, sender=ALICE, sender_name="Alice Example", subject="Lunch?")

    report = await mail.run_once()

    assert crm.logged == [
        {
            "contact_name": "Alice Example",
            "channel": "email",
            "direction": "incoming",
            "content_summary": "Alice Example <alice@example.com>: 'Lunch?'",
            "channel_identifier": ALICE,
        }
    ]
    assert report.crm_logged == 1
    assert bus.ids == [sent["id"]]
    entry = json.loads(log.path.read_text())["entries"][sent["id"]]
    assert entry["crmLoggedAt"]
    # Nothing new: nothing logged again.
    await mail.run_once()
    assert len(crm.logged) == 1


async def test_read_and_history_mail_is_never_logged(mail, delta, crm) -> None:
    delta.deliver(ME, sender=BOB, subject="Already read", is_read=True)
    delta.deliver(ME, sender=BOB, subject="Sent", folder="sentitems")
    await mail.run_once()
    assert crm.logged == []


async def test_a_410_resync_does_not_log_twice(mail, delta, crm, bus) -> None:
    old = [delta.deliver(ME, sender=ALICE, subject=f"Old {i}") for i in range(3)]
    await mail.run_once()
    assert len(crm.logged) == 3

    delta.expire_tokens()
    new = delta.deliver(ME, sender=BOB, subject="After the reset")
    report = await mail.run_once()

    assert report.mode == "resync"
    assert [p["channel_identifier"] for p in crm.logged] == [ALICE] * 3 + [BOB]
    assert bus.ids == [m["id"] for m in old] + [new["id"]]


async def test_a_round_that_dies_after_logging_does_not_log_again_on_retry(
    mail, delta, crm, bus, store
) -> None:
    sent = delta.deliver(ME, sender=ALICE, subject="Once")
    bus.fail_next = 1
    with pytest.raises(PublishFailed):
        await mail.run_once()
    assert len(crm.logged) == 1  # logged before the bus refused the event

    await mail.run_once()  # the retry publishes ...
    assert bus.ids == [sent["id"]]
    assert len(crm.logged) == 1  # ... and does not log it a second time


async def test_a_lost_seen_set_is_still_guarded_by_the_high_water_mark(
    mail, delta, crm, store
) -> None:
    delta.deliver(ME, sender=ALICE, subject="Old")
    await mail.run_once()
    store.seen_at.clear()
    delta.expire_tokens()
    await mail.run_once()
    assert len(crm.logged) == 1


async def test_a_crm_failure_never_blocks_the_pipeline(mail, delta, crm, bus, log) -> None:
    crm.raises = RuntimeError("database is down")
    sent = delta.deliver(ME, sender=ALICE, subject="Still published")

    report = await mail.run_once()

    assert bus.ids == [sent["id"]]
    assert report.crm_logged == 0
    entry = json.loads(log.path.read_text())["entries"][sent["id"]]
    assert "crmLoggedAt" not in entry


async def test_a_refused_interaction_is_not_counted_as_logged(mail, delta, crm, log) -> None:
    crm.ok = False
    sent = delta.deliver(ME, sender=ALICE, subject="Refused")
    report = await mail.run_once()
    assert report.crm_logged == 0
    assert "crmLoggedAt" not in json.loads(log.path.read_text())["entries"][sent["id"]]


async def test_without_a_crm_logger_nothing_is_logged(graph, store, bus, log, delta) -> None:
    ingestor = GraphMailIngestor(
        graph=graph,
        mailbox=ME,
        tenant_id=TENANT,
        store=store,
        publish=bus,
        email_log=log,
        now=lambda: delta.mail.clock,
    )
    delta.deliver(ME, sender=ALICE)
    report = await ingestor.run_once()
    assert report.published == 1 and report.crm_logged == 0
