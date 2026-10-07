"""The neutral types describe exactly what the gws handlers already produce."""

from __future__ import annotations

import base64

import pytest

from robothor.workspace.google import gmail_parse
from robothor.workspace.types import (
    GOOGLE_CAPABILITIES,
    CalendarRef,
    MailEnvelope,
    MailMessage,
    NormalizedEvent,
    SendResult,
    as_calendar_ref,
)


def _message() -> dict:
    body = base64.urlsafe_b64encode(b"hello").decode()
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "snippet": "hello",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "bob@example.com"},
                {"name": "Cc", "value": "carol@example.com"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": body}},
                {
                    "mimeType": "application/pdf",
                    "filename": "a.pdf",
                    "body": {"attachmentId": "x", "size": 3},
                },
            ],
        },
    }


def test_envelope_keys_match_the_shaper() -> None:
    shaped = gmail_parse._shape_envelope(_message())
    assert set(shaped) == set(MailEnvelope.__annotations__)


def test_message_keys_match_the_shaper() -> None:
    shaped = gmail_parse._shape_message(_message(), max_chars=100)
    assert set(shaped) == set(MailMessage.__annotations__)
    assert set(MailMessage.__required_keys__) == set(shaped) - {"attachments"}


def test_send_result_fields() -> None:
    assert set(SendResult.__annotations__) == {"id", "threadId", "labelIds", "internetMessageId"}
    assert SendResult.__required_keys__ == frozenset()


def test_normalized_event_is_a_google_v3_subset() -> None:
    fields = set(NormalizedEvent.__annotations__)
    for key in (
        "id",
        "etag",
        "status",
        "summary",
        "description",
        "location",
        "start",
        "end",
        "attendees",
        "organizer",
        "recurrence",
        "recurringEventId",
        "htmlLink",
        "hangoutLink",
        "conferenceData",
        "attendeesOmitted",
        "provider_extra",
    ):
        assert key in fields


def test_calendar_ref() -> None:
    ref = CalendarRef("primary", "own")
    assert ref.mailbox == ""
    assert as_calendar_ref(ref) is ref
    assert as_calendar_ref("alice@example.com") == CalendarRef("alice@example.com", "other")
    with pytest.raises(ValueError):
        CalendarRef("x", "someone")  # type: ignore[arg-type]


def test_google_capabilities() -> None:
    assert GOOGLE_CAPABILITIES["provider"] == "google"
    assert set(GOOGLE_CAPABILITIES["send_updates_modes"]) == {"all", "externalOnly", "none"}
    assert GOOGLE_CAPABILITIES["labels"] == "gmail_labels"
    assert GOOGLE_CAPABILITIES["online_meeting"] == "hangouts_meet"
