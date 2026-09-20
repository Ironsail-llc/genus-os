from copy import deepcopy

import pytest

from robothor.engine import calendar_attendees as calendar


@pytest.fixture
def api(monkeypatch):
    class Fake:
        event = {
            "id": "meeting",
            "etag": '"v1"',
            "status": "confirmed",
            "attendees": [
                {
                    "email": "robert@example.com",
                    "responseStatus": "accepted",
                    "optional": True,
                    "comment": "Looking forward to it",
                }
            ],
            "start": {"dateTime": "2026-09-22T16:00:00-04:00"},
            "end": {"dateTime": "2026-09-22T16:30:00-04:00"},
            "hangoutLink": "https://meet.example.test/test",
            "organizer": {"email": "owner@example.com"},
        }
        calls = []
        failure = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, calendar_id, event_id, **kwargs):
            self.calls.append((method, kwargs))
            if method == "PATCH":
                assert kwargs["etag"] == self.event["etag"]
                if self.failure:
                    return self.failure
                self.event.update(deepcopy(kwargs["body"]))
                self.event["etag"] = '"v2"'
            return deepcopy(self.event)

    fake = Fake()
    monkeypatch.setattr(calendar, "CalendarTransport", lambda: fake)
    return fake


def add(screen=lambda *a: None):
    return calendar.add_attendees(
        "owner@example.com", "meeting", ["sam@example.com"], screen=screen
    )


def test_add_preserves_attendee_objects_and_event(api):
    before = deepcopy(api.event)
    result = add()
    assert result["verification"] == "verified"
    assert result["invitations_requested"] is True
    assert result["delivery_verified"] is False
    assert api.event["attendees"][0] == before["attendees"][0]
    assert {k: v for k, v in api.event.items() if k not in {"attendees", "etag"}} == {
        k: v for k, v in before.items() if k not in {"attendees", "etag"}
    }
    assert [c[0] for c in api.calls] == ["GET", "PATCH", "GET"]


def test_repeated_confirmation_never_resends(api):
    add()
    result = add()
    assert result["status"] == "already_present"
    assert result["invitations_requested"] is False
    assert sum(c[0] == "PATCH" for c in api.calls) == 1


def test_screen_includes_existing_notification_recipients(api):
    seen = []
    result = add(lambda *recipients: seen.extend(recipients) or {"error": "do not contact"})
    assert set(seen) == {"sam@example.com", "robert@example.com"}
    assert "error" in result
    assert len(api.calls) == 1


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_http_failure_has_no_blind_retry(api, code):
    api.failure = {"error": "failed", "status_code": code}
    assert "error" in add()
    assert [c[0] for c in api.calls] == (
        ["GET", "PATCH", "GET"] if code >= 500 else ["GET", "PATCH"]
    )


def test_uncertain_write_is_only_read_back(api):
    api.failure = {"error": "timeout", "outcome_unknown": True}
    result = add()
    assert result["invitations_requested"] is None
    assert result["verification"] == "unverified"
    assert [c[0] for c in api.calls] == ["GET", "PATCH", "GET"]


def test_conflicting_edits_get_one_recovery(api):
    api.failure = {"error": "conflict", "status_code": 412}
    assert "error" in add()
    assert [c[0] for c in api.calls] == ["GET", "PATCH", "GET", "PATCH"]


def test_incomplete_attendees_are_not_overwritten(api):
    api.event["attendeesOmitted"] = True
    assert "error" in add()
    assert len(api.calls) == 1


def test_missing_etag_refuses_unconditional_mutation(api):
    del api.event["etag"]
    assert "error" in add()
    assert len(api.calls) == 1


def test_cancel_while_reading_prevents_later_background_write(api):
    from threading import Event

    cancelled = Event()
    original = api.request

    def read_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result

    api.request = read_then_cancel
    result = calendar.add_attendees(
        "owner@example.com",
        "meeting",
        ["sam@example.com"],
        screen=lambda *a: None,
        cancelled=cancelled,
    )
    assert "cancelled before write" in result["error"]
    assert [c[0] for c in api.calls] == ["GET"]
