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


def test_a_declined_guest_is_not_reported_as_invited(api):
    """`responseStatus` was ignored, so someone who said no was reported as
    already invited and the re-invite silently did nothing."""
    api.event["attendees"] = [{"email": "sam@example.com", "responseStatus": "declined"}]
    result = add()
    assert result["status"] == "already_present"
    assert result["declined"] == ["sam@example.com"]
    assert result["invitations_requested"] is False
    assert "declined" in result["note"].lower()
    assert sum(c[0] == "PATCH" for c in api.calls) == 0


def test_a_recurring_master_says_the_change_is_the_whole_series(api):
    """One guest on a 52-week master mails every existing guest, 52 times."""
    api.event["recurrence"] = ["RRULE:FREQ=WEEKLY;COUNT=52"]
    result = add()
    assert result["recurrence"]["scope"] == "series"
    assert result["recurrence"]["rules"] == ["RRULE:FREQ=WEEKLY;COUNT=52"]


def test_a_single_occurrence_says_which_one_it_changed(api):
    api.event["recurringEventId"] = "master"
    result = add()
    assert result["recurrence"]["scope"] == "instance"
    assert result["recurrence"]["series_id"] == "master"


def test_a_non_recurring_event_claims_no_series(api):
    assert add()["recurrence"] is None


def test_a_normalised_timezone_is_not_a_failed_write(api):
    """Google may echo the same instant in a different representation. Comparing
    the dicts turned a write that SUCCEEDED into a blocked operation."""
    api.event["start"] = {"dateTime": "2026-09-22T16:00:00-04:00", "timeZone": "America/New_York"}
    original = api.request

    def normalise(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "PATCH":
            api.event["start"] = {"dateTime": "2026-09-22T20:00:00Z"}
        return result

    api.request = normalise
    result = add()
    assert result["verification"] == "verified"
    assert result["status"] == "updated"
    assert "error" not in result


def test_a_real_time_change_is_still_caught(api):
    """The instant comparison must not become a blanket exemption."""
    original = api.request

    def move(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "PATCH":
            api.event["start"] = {"dateTime": "2026-09-22T17:00:00-04:00"}
        return result

    api.request = move
    result = add()
    assert result["verification"] == "unverified"
    assert "error" in result


@pytest.mark.parametrize(
    "args",
    [
        {"attendees": ["sam@example.com"]},
        {"event_id": "   ", "attendees": ["sam@example.com"]},
        {"event_id": 7, "attendees": ["sam@example.com"]},
        {"event_id": "meeting", "attendees": []},
        {"event_id": "meeting", "attendees": "sam@example.com"},
        {"event_id": "meeting", "attendees": ["sam at example.com"]},
        {"event_id": "meeting", "attendees": ["sam@example"]},
        {"event_id": "meeting", "attendees": [None]},
    ],
)
def test_the_tool_refuses_malformed_input_without_reaching_google(api, args):
    """The operation layer validates too, but the tool is reachable on its own."""
    from robothor.engine.tools.handlers import gws as gws_handlers

    out = gws_handlers._handle_gws_tool(
        "gws_calendar_add_attendees", {"calendar_id": "owner@example.com", **args}
    )
    assert "error" in out
    assert api.calls == []


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
