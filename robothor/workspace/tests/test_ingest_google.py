"""The Gmail history ingestor: a historyId cursor, email.new once per message, no replay."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.events import contract
from robothor.workspace.errors import WorkspaceError
from robothor.workspace.ingest.base import PublishFailed
from robothor.workspace.ingest.email_log import EmailLog
from robothor.workspace.ingest.google import PROVIDER, GmailHistoryIngestor
from robothor.workspace.ingest.state import MemoryIngestStore
from robothor.workspace.tests.fake_gmail_history import ME, FakeGmailHistory

TENANT = "test-tenant"


class Recorder:
    def __init__(self, fail_after: int | None = None) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_after = fail_after

    async def __call__(self, stream: str, event_type: str, payload: dict[str, Any]) -> None:
        if self.fail_after is not None and len(self.events) >= self.fail_after:
            raise PublishFailed("bus down")
        self.events.append((stream, event_type, payload))

    @property
    def ids(self) -> list[str]:
        return [p["id"] for _s, _t, p in self.events]


def _ingestor(gmail: FakeGmailHistory, store: MemoryIngestStore, publish: Any, **kw: Any):
    return GmailHistoryIngestor(
        gmail=gmail, mailbox=ME, tenant_id=TENANT, store=store, publish=publish, **kw
    )


async def _cursor(store: MemoryIngestStore) -> str:
    state = await store.load(TENANT, PROVIDER, ME, "mail")
    assert state is not None
    return state.delta_link


def test_identity() -> None:
    assert PROVIDER == "google"
    assert GmailHistoryIngestor.provider == "google"
    assert GmailHistoryIngestor.resource == "mail"


def test_requires_mailbox_and_tenant() -> None:
    with pytest.raises(ValueError):
        GmailHistoryIngestor(
            gmail=FakeGmailHistory(), mailbox="", tenant_id=TENANT, store=None, publish=None
        )
    with pytest.raises(ValueError):
        GmailHistoryIngestor(
            gmail=FakeGmailHistory(), mailbox=ME, tenant_id="", store=None, publish=None
        )


async def test_first_run_starts_at_the_profile_history_id_without_replay() -> None:
    gmail = FakeGmailHistory(history_id=500)
    gmail.deliver("old-1")  # already in the mailbox before the ingest existed
    store, publish = MemoryIngestStore(), Recorder()

    report = await _ingestor(gmail, store, publish).run_once()

    assert report.mode == "initial"
    assert report.published == 0
    assert publish.events == []
    assert gmail.history_calls == []
    assert await _cursor(store) == "501"


async def test_incremental_round_publishes_each_new_inbox_message_once() -> None:
    gmail = FakeGmailHistory(history_id=100, page_size=2)
    store, publish = MemoryIngestStore(), Recorder()
    ingestor = _ingestor(gmail, store, publish)
    await ingestor.run_once()  # baseline at 100

    for mid in ("m1", "m2", "m3"):
        gmail.deliver(mid)
    report = await ingestor.run_once()

    assert report.mode == "incremental"
    assert report.read == 3
    assert report.published == 3
    assert publish.ids == ["m1", "m2", "m3"]
    # Pages were followed: three records at two per page.
    assert gmail.history_calls == [("100", None), ("100", "2")]
    assert await _cursor(store) == "103"
    for stream, event_type, payload in publish.events:
        assert (stream, event_type) == (contract.EMAIL_STREAM, contract.EMAIL_NEW)
        contract.validate("email_new", payload)
        assert payload["provider"] == "google"
        assert payload["from"] == "Alice <alice@example.com>"
        assert payload["subject"] == "Hello"
        assert payload["threadId"] == f"t-{payload['id']}"
        assert "INBOX" in payload["labels"]

    again = await ingestor.run_once()
    assert again.published == 0
    assert gmail.history_calls[-1] == ("103", None)


async def test_the_seen_set_stops_a_replayed_history_from_publishing_twice() -> None:
    gmail = FakeGmailHistory(history_id=100)
    store, publish = MemoryIngestStore(), Recorder()
    ingestor = _ingestor(gmail, store, publish)
    await ingestor.run_once()
    gmail.deliver("m1")
    await ingestor.run_once()

    # Roll the cursor back, as a crash before the save would leave it.
    state = await store.load(TENANT, PROVIDER, ME, "mail")
    assert state is not None
    await store.save(state.with_(delta_link="100"))
    gmail.deliver("m2")
    await ingestor.run_once()

    assert publish.ids == ["m1", "m2"]


async def test_a_failed_publish_keeps_the_cursor_and_the_next_round_finishes() -> None:
    gmail = FakeGmailHistory(history_id=100)
    store = MemoryIngestStore()
    await _ingestor(gmail, store, Recorder()).run_once()
    gmail.deliver("m1")
    gmail.deliver("m2")

    flaky = Recorder(fail_after=1)
    with pytest.raises(PublishFailed):
        await _ingestor(gmail, store, flaky).run_once()
    assert flaky.ids == ["m1"]
    assert await _cursor(store) == "100"

    healthy = Recorder()
    await _ingestor(gmail, store, healthy).run_once()
    assert healthy.ids == ["m2"]
    assert await _cursor(store) == "102"


async def test_an_expired_cursor_resets_to_the_profile_without_replaying() -> None:
    gmail = FakeGmailHistory(history_id=100)
    store, publish = MemoryIngestStore(), Recorder()
    ingestor = _ingestor(gmail, store, publish)
    await ingestor.run_once()
    gmail.deliver("lost-in-the-gap")
    gmail.expired = True

    report = await ingestor.run_once()

    assert report.mode == "resync"
    assert report.published == 0
    assert publish.events == []
    assert await _cursor(store) == "101"

    gmail.expired = False
    gmail.deliver("m-after")
    await ingestor.run_once()
    assert publish.ids == ["m-after"]


async def test_other_history_errors_propagate_and_keep_the_cursor() -> None:
    gmail = FakeGmailHistory(history_id=100)
    store = MemoryIngestStore()
    ingestor = _ingestor(gmail, store, Recorder())
    await ingestor.run_once()

    async def broken(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise WorkspaceError("backend error", status=500)

    gmail.history = broken  # type: ignore[method-assign]
    with pytest.raises(WorkspaceError):
        await ingestor.run_once()
    assert await _cursor(store) == "100"


async def test_only_unread_inbox_mail_is_published() -> None:
    gmail = FakeGmailHistory(history_id=100, page_size=10)
    store, publish = MemoryIngestStore(), Recorder()
    ingestor = _ingestor(gmail, store, publish)
    await ingestor.run_once()

    gmail.deliver("new", labels=("INBOX", "UNREAD"))
    gmail.deliver("draft", labels=("DRAFT",))
    gmail.deliver("sent", labels=("SENT",))
    gmail.deliver("read", labels=("INBOX",))
    gmail.deliver("deleted")
    del gmail.messages["deleted"]  # gone before the round could read it

    report = await ingestor.run_once()

    assert publish.ids == ["new"]
    assert report.published == 1
    assert "draft" not in gmail.message_calls
    assert "sent" not in gmail.message_calls
    assert await _cursor(store) == "105"


async def test_new_mail_is_merged_into_the_email_log(tmp_path) -> None:
    gmail = FakeGmailHistory(history_id=100)
    store, publish = MemoryIngestStore(), Recorder()
    log = EmailLog(tmp_path / "email-log.json")
    ingestor = _ingestor(gmail, store, publish, email_log=log)
    await ingestor.run_once()
    gmail.deliver("m1")

    report = await ingestor.run_once()

    assert report.logged == 1
    entries = json.loads((tmp_path / "email-log.json").read_text())["entries"]
    assert entries["m1"]["provider"] == "google"
    assert entries["m1"]["threadId"] == "t-m1"
    assert entries["m1"]["from"] == "Alice <alice@example.com>"


async def test_new_mail_is_logged_to_the_crm_once() -> None:
    gmail = FakeGmailHistory(history_id=100)
    store, publish = MemoryIngestStore(), Recorder()
    logged: list[dict[str, Any]] = []

    async def log_interaction(payload: dict[str, Any]) -> bool:
        logged.append(payload)
        return True

    ingestor = _ingestor(gmail, store, publish, log_interaction=log_interaction)
    await ingestor.run_once()
    gmail.deliver("m1", sender="Bob <bob@example.com>", subject="Lunch")

    report = await ingestor.run_once()

    assert report.crm_logged == 1
    assert logged == [
        {
            "contact_name": "Bob",
            "channel": "email",
            "direction": "incoming",
            "content_summary": "Bob <bob@example.com>: 'Lunch'",
            "channel_identifier": "bob@example.com",
        }
    ]
