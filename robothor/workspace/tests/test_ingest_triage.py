"""``triage-inbox.json``: the small file the email classifier and calendar monitor read.

The Google instance's sync script rebuilds it after every sync from
``email-log.json``, ``calendar-log.json`` and ``jira-log.json``. The
Microsoft 365 worker builds the same shape (:mod:`robothor.workspace.ingest.triage`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from robothor.events import contract
from robothor.workspace.ingest.triage import TriageInbox, build_triage_inbox, default_path

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def _entry(eid: str, **fields) -> dict:
    base = {
        "id": eid,
        "threadId": f"conv-{eid}",
        "fetchedAt": "2026-10-06T14:00:00+00:00",
        "readAt": None,
        "from": "Alice Example <alice@example.com>",
        "subject": f"Subject {eid}",
        "date": "Tue, 06 Oct 2026 14:00:00 +0000",
        "labels": ["UNREAD", "INBOX"],
        "snippet": None,
        "categorizedAt": None,
        "urgency": None,
        "category": None,
        "actionRequired": None,
        "actionCompletedAt": None,
        "pendingReviewAt": None,
        "reviewedAt": None,
        "messageCount": 1,
        "provider": "microsoft365",
    }
    base.update(fields)
    return base


def _log(*entries: dict) -> dict:
    return {"lastCheckedAt": None, "entries": {e["id"]: e for e in entries}}


def test_uncategorized_mail_is_a_new_item_with_the_script_fields() -> None:
    inbox = build_triage_inbox(_log(_entry("m1")), {}, {}, [], now=NOW)

    contract.validate("triage_inbox", inbox)
    assert inbox["counts"] == {"emails": 1, "calendar": 0, "jira": 0, "total": 1}
    assert inbox["activeEscalationIds"] == []
    assert inbox["preparedAt"] == NOW.isoformat()
    [item] = inbox["items"]
    assert item == {
        "source": "email",
        "type": "new",
        "id": "m1",
        "threadId": "conv-m1",
        "from": "Alice Example <alice@example.com>",
        "subject": "Subject m1",
        "date": "Tue, 06 Oct 2026 14:00:00 +0000",
        "labels": ["UNREAD", "INBOX"],
        "snippet": None,
        "messageCount": 1,
    }


def test_a_categorized_entry_is_out_unless_its_follow_up_is_due() -> None:
    due = (NOW - timedelta(hours=1)).isoformat()
    later = (NOW + timedelta(hours=1)).isoformat()
    stamp = (NOW - timedelta(days=1)).isoformat()
    log = _log(
        _entry("done", categorizedAt=stamp),
        _entry("due", categorizedAt=stamp, pendingReviewAt=due),
        _entry("later", categorizedAt=stamp, pendingReviewAt=later),
        _entry("reviewed", categorizedAt=stamp, pendingReviewAt=due, reviewedAt=stamp),
        _entry("nosender", **{"from": None}),
    )
    inbox = build_triage_inbox(log, {}, {}, [], now=NOW)
    contract.validate("triage_inbox", inbox)
    by_id = {i["id"]: i for i in inbox["items"]}
    assert set(by_id) == {"due", "nosender"}
    assert by_id["due"]["type"] == "follow-up"
    assert by_id["due"]["pendingReviewAt"] == due
    assert by_id["nosender"]["needsBackfill"] is True


def test_escalated_threads_are_filtered_and_listed() -> None:
    log = _log(_entry("m1"), _entry("m2", threadId="conv-escalated"), _entry("m3"))
    inbox = build_triage_inbox(log, {}, {}, ["m1", "conv-escalated"], now=NOW)
    assert [i["id"] for i in inbox["items"]] == ["m3"]
    assert inbox["activeEscalationIds"] == ["m1", "conv-escalated"]
    assert inbox["counts"]["emails"] == 1


def test_calendar_and_jira_logs_are_folded_in_when_present() -> None:
    recent = (NOW - timedelta(hours=1)).isoformat()
    stale = (NOW - timedelta(days=3)).isoformat()
    calendar = {
        "meetings": [
            {"id": "ev1", "title": "Sync", "start": "2026-10-07T15:00:00Z", "fetchedAt": recent},
            {"id": "ev2", "title": "Old", "start": "2026-10-07T16:00:00Z", "fetchedAt": stale},
            {"id": "ev3", "title": "Moved", "start": "2026-10-08T09:00:00Z", "fetchedAt": recent},
            {"id": "ev4", "title": "Done", "categorizedAt": recent, "fetchedAt": recent},
        ],
        "changes": [
            {
                "eventId": "ev3",
                "title": "Moved",
                "type": "rescheduled",
                "start": "2026-10-08T09:00:00Z",
                "end": "2026-10-08T09:30:00Z",
                "timestamp": recent,
            },
            {"eventId": "ev5", "title": "No start", "type": "new", "timestamp": recent},
        ],
    }
    jira = {
        "pendingActions": [
            {"ticket": "OPS-1", "action": "review", "summary": "Check"},
            {"ticket": "OPS-2", "action": "close", "completedAt": recent},
        ]
    }
    inbox = build_triage_inbox(_log(), calendar, jira, [], now=NOW)
    contract.validate("triage_inbox", inbox)
    assert inbox["counts"] == {"emails": 0, "calendar": 2, "jira": 1, "total": 3}
    kinds = [(i["source"], i["type"], i.get("id") or i.get("ticket")) for i in inbox["items"]]
    assert kinds == [
        ("calendar", "meeting", "ev1"),
        ("calendar", "change", "ev3"),
        ("jira", "pending-action", "OPS-1"),
    ]


async def test_rebuild_writes_next_to_the_email_log_atomically(tmp_path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    email_log = memory / "email-log.json"
    email_log.write_text(json.dumps(_log(_entry("m1"))))
    (memory / "jira-log.json").write_text(json.dumps({"pendingActions": [{"ticket": "OPS-1"}]}))
    calls: list[int] = []

    async def escalations() -> list[str]:
        calls.append(1)
        return []

    triage = TriageInbox(default_path(email_log), email_log_path=email_log, escalations=escalations)
    assert triage.path == memory / "triage-inbox.json"

    inbox = await triage.rebuild(now=NOW)

    on_disk = json.loads(triage.path.read_text())
    assert on_disk == inbox
    assert on_disk["counts"] == {"emails": 1, "calendar": 0, "jira": 1, "total": 2}
    assert calls == [1]
    # No temp file is left behind.
    assert sorted(p.name for p in memory.iterdir()) == [
        "email-log.json",
        "jira-log.json",
        "triage-inbox.json",
    ]


async def test_rebuild_survives_missing_logs_and_a_failing_escalation_lookup(tmp_path) -> None:
    async def broken() -> list[str]:
        raise RuntimeError("database is down")

    path = tmp_path / "memory" / "triage-inbox.json"
    triage = TriageInbox(
        path, email_log_path=tmp_path / "memory" / "email-log.json", escalations=broken
    )
    inbox = await triage.rebuild(now=NOW)
    assert inbox["counts"]["total"] == 0
    assert json.loads(path.read_text())["items"] == []


def test_the_configured_path_wins(monkeypatch, tmp_path) -> None:
    from robothor.settings import reset_settings

    target = tmp_path / "elsewhere" / "triage.json"
    monkeypatch.setenv("ROBOTHOR_TRIAGE_INBOX_PATH", str(target))
    reset_settings()
    try:
        assert default_path(tmp_path / "email-log.json") == target
    finally:
        monkeypatch.delenv("ROBOTHOR_TRIAGE_INBOX_PATH")
        reset_settings()
    assert default_path(tmp_path / "email-log.json") == tmp_path / "triage-inbox.json"
