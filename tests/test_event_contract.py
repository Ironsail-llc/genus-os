"""The written-down email/calendar event contract matches what producers send.

The samples below are SYNTHETIC: shaped exactly like what the instance Google
sync publishes (``email.new`` with id/from/subject/date/labels, an
email-log.json entry from its minimal-entry builder, a calendar-log meeting),
with made-up values. If the schema stops accepting them, the platform contract
has drifted from the producer every consumer was written against.
"""

from __future__ import annotations

import pytest

from robothor.events import contract

GOOGLE_EMAIL_NEW = {
    "id": "19a0f00000000001",
    "from": "Alice Example <alice@example.com>",
    "subject": "Quarterly numbers",
    "date": "Mon, 05 Oct 2026 09:12:00 -0400",
    "labels": ["UNREAD", "INBOX", "CATEGORY_PERSONAL"],
}

GOOGLE_LOG_ENTRY = {
    "id": "19a0f00000000001",
    "threadId": "19a0f00000000001",
    "fetchedAt": "2026-10-05T09:15:02.123456",
    "readAt": None,
    "from": "Alice Example <alice@example.com>",
    "subject": "Quarterly numbers",
    "date": "Mon, 05 Oct 2026 09:12:00 -0400",
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
    "crmLoggedAt": "2026-10-05T09:15:03.000001",
}


#: triage-inbox.json as the Google sync's build_triage_inbox writes it.
GOOGLE_TRIAGE_INBOX = {
    "preparedAt": "2026-10-05T13:15:05.000001+00:00",
    "counts": {"emails": 2, "calendar": 2, "jira": 1, "total": 5},
    "items": [
        {
            "source": "email",
            "type": "new",
            "id": "19a0f00000000001",
            "from": "Alice Example <alice@example.com>",
            "subject": "Quarterly numbers",
            "date": "Mon, 05 Oct 2026 09:12:00 -0400",
            "labels": ["UNREAD", "INBOX"],
            "snippet": None,
            "messageCount": 1,
        },
        {
            "source": "email",
            "type": "follow-up",
            "id": "19a0f00000000002",
            "from": "bob@example.com",
            "subject": "Re: contract",
            "date": "Sun, 04 Oct 2026 10:00:00 -0400",
            "pendingReviewAt": "2026-10-05T12:00:00+00:00",
        },
        {
            "source": "calendar",
            "type": "meeting",
            "id": "evt-0001",
            "title": "Planning",
            "start": "2026-10-06T14:00:00Z",
            "startLocal": "2026-10-06T10:00:00-04:00",
            "end": "2026-10-06T14:30:00Z",
            "endLocal": "2026-10-06T10:30:00-04:00",
            "attendees": ["alice@example.com"],
        },
        {
            "source": "calendar",
            "type": "change",
            "id": "evt-0002",
            "title": "Moved",
            "changeType": "rescheduled",
            "details": None,
            "start": "2026-10-07T14:00:00Z",
            "end": "2026-10-07T14:30:00Z",
            "detectedAt": "2026-10-05T13:00:00+00:00",
        },
        {
            "source": "jira",
            "type": "pending-action",
            "ticket": "OPS-1",
            "action": "review",
            "summary": "Check the rollout",
        },
    ],
    "activeEscalationIds": ["19a0f00000000009"],
}


def test_the_google_triage_inbox_matches_the_schema() -> None:
    contract.validate("triage_inbox", GOOGLE_TRIAGE_INBOX)


def test_the_google_email_new_payload_matches_the_schema() -> None:
    contract.validate("email_new", GOOGLE_EMAIL_NEW)


def test_the_google_email_log_matches_the_schema() -> None:
    log = {
        "lastCheckedAt": "2026-10-05T09:15:04",
        "entries": {"19a0f00000000001": GOOGLE_LOG_ENTRY},
    }
    contract.validate("email_log", log)
    contract.validate("email_log", {"lastCheckedAt": None, "entries": {}})


def test_the_payload_builder_reproduces_the_google_payload() -> None:
    entry = dict(GOOGLE_LOG_ENTRY, labels=GOOGLE_EMAIL_NEW["labels"])
    payload = contract.email_new_payload(entry)
    assert {k: payload[k] for k in GOOGLE_EMAIL_NEW} == GOOGLE_EMAIL_NEW
    contract.validate("email_new", payload)


def test_a_calendar_log_meeting_plus_change_type_matches_the_schema() -> None:
    meeting = {
        "id": "evt-0001",
        "title": "Planning",
        "start": "2026-10-06T14:00:00Z",
        "end": "2026-10-06T14:30:00Z",
        "attendees": ["alice@example.com", "bob@example.com"],
        "hangoutLink": "",
    }
    for change in contract.CALENDAR_EVENT_TYPES:
        contract.validate("calendar_event", dict(meeting, change_type=change))


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("email_new", {k: v for k, v in GOOGLE_EMAIL_NEW.items() if k != "id"}),
        ("email_new", dict(GOOGLE_EMAIL_NEW, labels="INBOX")),
        ("email_new", dict(GOOGLE_EMAIL_NEW, id="")),
        (
            "calendar_event",
            {
                "id": "e",
                "change_type": "exploded",
                "title": "",
                "start": "",
                "end": "",
                "attendees": [],
            },
        ),
        ("email_log", {"entries": {}}),
        ("triage_inbox", {"counts": {}, "items": []}),
        (
            "triage_inbox",
            dict(GOOGLE_TRIAGE_INBOX, items=[{"source": "email", "type": "new"}]),
        ),
    ],
)
def test_the_schema_rejects_a_drifted_payload(name, bad) -> None:
    with pytest.raises(contract.ContractError):
        contract.validate(name, bad)


def test_the_calendar_event_types_are_the_ones_the_consumers_listen_for() -> None:
    from pathlib import Path

    import yaml

    pipeline = Path(__file__).resolve().parents[1] / "docs" / "workflows" / "calendar-pipeline.yaml"
    hooked = {
        t["event_type"]
        for t in yaml.safe_load(pipeline.read_text())["triggers"]
        if t.get("type") == "hook"
    }
    assert hooked <= set(contract.CALENDAR_EVENT_TYPES.values())
    assert contract.CALENDAR_CANCELLATION == "calendar.cancellation"  # consumers/calendar.py
