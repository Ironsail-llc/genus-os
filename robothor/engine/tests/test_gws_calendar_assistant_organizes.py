"""The assistant organizes the meetings it books.

Since the 2026-09-16 itinerary fix, ``gws_calendar_create`` with no calendar
argument wrote every event onto the OPERATOR's calendar, operator as organizer,
and invited nobody else from the assistant's side. That was right for an
itinerary and wrong for a meeting: the assistant booked a call with two outside
guests on the operator's calendar, the guest list was the two guests, and when
the call ended the Google Meet notes and transcript belonged to the operator.
The assistant, which had booked the meeting in order to follow it up, could not
read a word of it.

The rule now:

* an event with at least one guest other than the operator, and no explicit
  ``calendar`` / ``calendar_id``, goes on the ASSISTANT's own calendar — the
  assistant is the organizer and owns the notes; the operator is invited;
* a guest-less event (an itinerary leg, a hold, a personal block) still goes on
  the operator's calendar with nobody invited — the 2026-09-16 fix stands;
* ``calendar=`` / ``calendar_id=`` always win.

And for a Google Meet the assistant organizes, transcription and smart notes are
switched on through the Meet REST API — best-effort, never at the cost of the
booking.

Every test fakes ``_run_gws``. Nothing reaches Google. Every address is a
generic fixture.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws as gws_handlers

OPERATOR_EMAIL = "alice@example.com"
ASSISTANT_EMAIL = "bot@example.com"
GUEST = "bob@example.com"
MEETING_CODE = "abc-defg-hij"
SPACE_NAME = "spaces/XyZ123space"


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import yaml

    path = tmp_path / "owner.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tenant_id": "fixture-tenant",
                "first_name": "Alice",
                "last_name": "Example",
                "email": OPERATOR_EMAIL,
            }
        )
    )
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(path))
    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", ASSISTANT_EMAIL)
    monkeypatch.setattr(gws_handlers, "ROBOTHOR_EMAIL", ASSISTANT_EMAIL)


class Recorder:
    """A fake ``_run_gws``: records argv, answers per the first three words."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.meet_get: Any = {"name": SPACE_NAME, "meetingCode": MEETING_CODE}
        self.meet_patch: Any = {"name": SPACE_NAME}
        self.conference = True

    def __call__(self, args: list[str], timeout: int = 30) -> Any:
        entry: dict[str, Any] = {"argv": args}
        if "--params" in args:
            entry["params"] = json.loads(args[args.index("--params") + 1])
        if "--json" in args:
            entry["body"] = json.loads(args[args.index("--json") + 1])
        self.calls.append(entry)
        head = args[:3]
        if head == ["calendar", "events", "list"]:
            return {"items": []}
        if head == ["calendar", "events", "insert"]:
            out: dict[str, Any] = {
                "id": "evt-1",
                "htmlLink": "https://calendar.example.com/event?eid=evt-1",
                "status": "confirmed",
            }
            if self.conference and "conferenceData" in entry.get("body", {}):
                out["hangoutLink"] = f"https://meet.google.com/{MEETING_CODE}"
                out["conferenceData"] = {
                    "conferenceId": MEETING_CODE,
                    "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": f"https://meet.google.com/{MEETING_CODE}",
                        }
                    ],
                }
            return out
        if head == ["meet", "spaces", "get"]:
            if isinstance(self.meet_get, Exception):
                raise self.meet_get
            return self.meet_get
        if head == ["meet", "spaces", "patch"]:
            if isinstance(self.meet_patch, Exception):
                raise self.meet_patch
            return self.meet_patch
        return {}

    def of(self, *head: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if tuple(c["argv"][: len(head)]) == head]

    def insert(self) -> dict[str, Any]:
        (only,) = self.of("calendar", "events", "insert")
        return only


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(gws_handlers, "_run_gws", rec)
    monkeypatch.setattr(gws_handlers, "_record_calendar_event", lambda **k: None)
    monkeypatch.setattr(gws_handlers, "_dnc_refusal", lambda *a, **k: None)
    monkeypatch.delenv("ROBOTHOR_CALENDAR_MEET_ARTIFACTS", raising=False)
    return rec


def _create(**kwargs: Any) -> dict[str, Any]:
    base = {
        "summary": "Intro call",
        "start": "2026-10-01T15:00:00Z",
        "end": "2026-10-01T15:30:00Z",
    }
    return gws_handlers._handle_gws_tool("gws_calendar_create", {**base, **kwargs})


# ── A1: whose calendar a meeting goes on ──────────────────────────────


class TestTheAssistantOrganizesMeetingsWithGuests:
    def test_a_meeting_with_guests_goes_on_the_assistants_calendar(
        self, operator, recorder
    ) -> None:
        """The incident: two outside guests, no calendar argument. The event
        must be the assistant's, so the notes and transcript are too."""
        out = _create(attendees=[GUEST, "carol@example.com"], with_meet=False)

        assert recorder.insert()["params"]["calendarId"] == "primary"
        assert out["calendar"] == {"kind": "own", "id": "primary"}

    def test_the_operator_is_invited_to_it(self, operator, recorder) -> None:
        _create(attendees=[GUEST], with_meet=False)
        ins = recorder.insert()

        assert [a["email"] for a in ins["body"]["attendees"]] == [GUEST, OPERATOR_EMAIL]
        assert ins["params"]["sendUpdates"] == "all"

    def test_the_result_names_everyone_invited(self, operator, recorder) -> None:
        out = _create(attendees=[GUEST], with_meet=False)

        assert out["invitations_requested"] is True
        assert out["attendees_notified"] == [GUEST, OPERATOR_EMAIL]

    def test_the_dedup_read_looks_at_the_assistants_calendar(self, operator, recorder) -> None:
        """Against the calendar actually chosen — a duplicate on the
        assistant's calendar is invisible from the operator's."""
        _create(attendees=[GUEST], with_meet=False)
        (listed,) = recorder.of("calendar", "events", "list")

        assert listed["params"]["calendarId"] == "primary"

    def test_the_do_not_contact_screen_sees_the_invited_operator(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, ...]] = []

        def _screen(tool: str, *addresses: str, **kwargs: Any) -> None:
            seen.append(tuple(addresses))

        monkeypatch.setattr(gws_handlers, "_dnc_refusal", _screen)
        _create(attendees=[GUEST], with_meet=False)

        assert seen == [(GUEST, OPERATOR_EMAIL)]


class TestTheItineraryFixStands:
    def test_a_guestless_event_stays_on_the_operators_calendar(self, operator, recorder) -> None:
        out = _create(summary="Flight to Lisbon", with_meet=False)
        ins = recorder.insert()

        assert ins["params"]["calendarId"] == OPERATOR_EMAIL
        assert "attendees" not in ins["body"]
        assert "sendUpdates" not in ins["params"]
        assert out["calendar"]["kind"] == "operator"

    def test_an_event_whose_only_guest_is_the_operator_stays_theirs(
        self, operator, recorder
    ) -> None:
        """Listing the operator is not inviting a guest: nobody else is there
        for the assistant to organize a meeting with."""
        _create(attendees=[OPERATOR_EMAIL.upper()], with_meet=False)

        assert recorder.insert()["params"]["calendarId"] == OPERATOR_EMAIL

    def test_blank_attendee_entries_are_not_guests(self, operator, recorder) -> None:
        _create(attendees=["", None], with_meet=False)

        assert recorder.insert()["params"]["calendarId"] == OPERATOR_EMAIL


class TestAnExplicitChoiceAlwaysWins:
    def test_calendar_operator_forces_the_operators_calendar(self, operator, recorder) -> None:
        out = _create(calendar="operator", attendees=[GUEST], with_meet=False)
        ins = recorder.insert()

        assert ins["params"]["calendarId"] == OPERATOR_EMAIL
        # The organiser is not made an attendee of their own meeting.
        assert [a["email"] for a in ins["body"]["attendees"]] == [GUEST]
        assert out["calendar"]["kind"] == "operator"

    def test_calendar_own_without_guests_is_still_own(self, operator, recorder) -> None:
        _create(calendar="own", with_meet=False)

        assert recorder.insert()["params"]["calendarId"] == "primary"

    def test_an_explicit_calendar_id_wins(self, operator, recorder) -> None:
        out = _create(calendar_id="team@example.com", attendees=[GUEST], with_meet=False)

        assert recorder.insert()["params"]["calendarId"] == "team@example.com"
        assert out["calendar"]["kind"] == "other"

    def test_an_invalid_calendar_is_still_refused(self, operator, recorder) -> None:
        out = _create(calendar="primary", attendees=[GUEST])

        assert out["hint"] == "invalid_params"
        assert recorder.calls == []


class TestWithNoOperatorConfigured:
    def test_it_falls_back_to_own_as_before(
        self, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gws_handlers, "_resolve_owner_email", lambda: "")
        _create(attendees=[GUEST], with_meet=False)
        ins = recorder.insert()

        assert ins["params"]["calendarId"] == "primary"
        assert [a["email"] for a in ins["body"]["attendees"]] == [GUEST]


class TestTheOtherToolsKeepTheOperatorDefault:
    """Only create chooses by guest list. A read or a cancel names an existing
    calendar; nothing about it changed."""

    def test_list_still_reads_the_operators_calendar(self, operator, recorder) -> None:
        gws_handlers._handle_gws_tool("gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"})
        (listed,) = recorder.of("calendar", "events", "list")

        assert listed["params"]["calendarId"] == OPERATOR_EMAIL


class _ProbeCalendar:
    """A fake blocking calendar whose ``session().get`` answers the own-calendar probe."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.gets: list[tuple[str, str]] = []

    def resolve(self, kind: str, *, address: str = "") -> Any:
        from robothor.workspace.types import CalendarRef

        if kind == "own":
            return CalendarRef("primary", "own")
        return CalendarRef(address, "operator", mailbox=address)

    def delete(self, ref: Any, event_id: str, *, send_updates: str) -> dict[str, Any]:
        return {}

    def session(self) -> Any:
        from contextlib import contextmanager

        outer = self

        class _Api:
            def get(self, ref: Any, event_id: str) -> Any:
                outer.gets.append((ref.calendar_id, event_id))
                if isinstance(outer.answer, Exception):
                    raise outer.answer
                return outer.answer

        @contextmanager
        def _cm() -> Any:
            yield _Api()

        return _cm()


class TestEditsFollowTheOrganizer:
    """An edit or cancel with no calendar argument reaches the calendar that
    organizes the event: the assistant's for a meeting it booked, the
    operator's otherwise."""

    def _ref(self, monkeypatch: pytest.MonkeyPatch, answer: Any, **args: Any) -> Any:
        fake = _ProbeCalendar(answer)
        monkeypatch.setattr(gws_handlers, "_calendar", lambda: fake)
        return gws_handlers._resolve_existing_event_ref(args, "evt-1"), fake

    def test_a_meeting_the_assistant_organizes_is_edited_on_its_calendar(
        self, operator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref, fake = self._ref(
            monkeypatch, {"id": "evt-1", "organizer": {"email": ASSISTANT_EMAIL, "self": True}}
        )
        assert (ref.calendar_id, ref.kind) == ("primary", "own")
        assert fake.gets == [("primary", "evt-1")]

    def test_an_invitation_the_assistant_only_attends_stays_on_the_operators(
        self, operator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref, _ = self._ref(monkeypatch, {"id": "evt-1", "organizer": {"email": OPERATOR_EMAIL}})
        assert ref.kind == "operator"

    @pytest.mark.parametrize(
        "answer", [{"error": "Calendar HTTP 404", "status_code": 404}, RuntimeError("boom")]
    )
    def test_not_found_or_a_failed_probe_keeps_the_default(
        self, operator, monkeypatch: pytest.MonkeyPatch, answer: Any
    ) -> None:
        ref, _ = self._ref(monkeypatch, answer)
        assert (ref.calendar_id, ref.kind) == (OPERATOR_EMAIL, "operator")

    def test_an_explicit_calendar_is_not_second_guessed(
        self, operator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref, fake = self._ref(monkeypatch, {"organizer": {"self": True}}, calendar="operator")
        assert ref.kind == "operator"
        assert fake.gets == []

    def test_update_and_delete_use_it(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used: list[str] = []
        real = gws_handlers._resolve_existing_event_ref

        def _spy(args: dict[str, Any], event_id: str) -> Any:
            used.append(event_id)
            return real(args, event_id)

        monkeypatch.setattr(gws_handlers, "_resolve_existing_event_ref", _spy)
        fake = _ProbeCalendar({"organizer": {"self": True}})
        monkeypatch.setattr(gws_handlers, "_calendar", lambda: fake)
        out = gws_handlers._handle_gws_tool("gws_calendar_delete", {"event_id": "evt-9"})
        assert used == ["evt-9"]
        assert out["calendar"]["kind"] == "own"


# ── A2: transcription on for meetings the assistant organizes ─────────


class TestTranscriptionIsTurnedOn:
    def test_a_meet_the_assistant_organizes_gets_transcription_and_notes(
        self, operator, recorder
    ) -> None:
        out = _create(attendees=[GUEST])

        (get,) = recorder.of("meet", "spaces", "get")
        assert get["params"] == {"name": f"spaces/{MEETING_CODE}"}
        (patch,) = recorder.of("meet", "spaces", "patch")
        assert patch["params"]["name"] == SPACE_NAME
        mask = set(patch["params"]["updateMask"].split(","))
        assert mask == {
            "config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration",
            "config.artifactConfig.smartNotesConfig.autoSmartNotesGeneration",
        }
        assert patch["body"] == {
            "config": {
                "artifactConfig": {
                    "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
                    "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
                }
            }
        }
        assert out["transcription"] == "enabled"
        assert out["id"] == "evt-1"

    def test_the_meeting_code_is_read_from_the_link_when_no_conference_id(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.workspace.google.adapter import meet_code_of

        assert meet_code_of({"hangoutLink": f"https://meet.google.com/{MEETING_CODE}"}) == (
            MEETING_CODE
        )
        assert meet_code_of({"conferenceData": {"conferenceId": MEETING_CODE}}) == MEETING_CODE
        assert meet_code_of({}) == ""
        assert meet_code_of({"hangoutLink": "https://example.com/not-meet"}) == ""

    def test_a_missing_scope_is_a_note_not_a_failure(self, operator, recorder) -> None:
        """The credential may not carry meetings.space.settings yet. The booking
        stands; the result says transcription is not on and why."""
        recorder.meet_get = {
            "error": "Request had insufficient authentication scopes.",
            "hint": "permission",
        }
        out = _create(attendees=[GUEST])

        assert "error" not in out
        assert out["id"] == "evt-1"
        assert out["transcription"].startswith("not_enabled: ")
        assert "insufficient authentication scopes" in out["transcription"]
        assert len(recorder.of("calendar", "events", "insert")) == 1
        assert recorder.of("meet", "spaces", "patch") == []

    def test_a_failed_patch_is_a_note_not_a_failure(self, operator, recorder) -> None:
        recorder.meet_patch = {"error": "Forbidden", "hint": "permission"}
        out = _create(attendees=[GUEST])

        assert out["id"] == "evt-1"
        assert out["transcription"] == "not_enabled: Forbidden"
        assert len(recorder.of("calendar", "events", "insert")) == 1

    def test_an_exception_in_the_meet_call_never_loses_the_booking(
        self, operator, recorder
    ) -> None:
        recorder.meet_get = RuntimeError("boom")
        out = _create(attendees=[GUEST])

        assert out["id"] == "evt-1"
        assert out["transcription"].startswith("not_enabled: ")
        assert len(recorder.of("calendar", "events", "insert")) == 1

    def test_no_meet_call_for_an_event_on_the_operators_calendar(self, operator, recorder) -> None:
        """The operator's meeting space follows the operator's settings; the
        assistant does not reach into it."""
        out = _create(calendar="operator", attendees=[GUEST])

        assert recorder.of("meet") == []
        assert "transcription" not in out

    def test_no_meet_call_without_a_conference(self, operator, recorder) -> None:
        out = _create(attendees=[GUEST], with_meet=False)

        assert recorder.of("meet") == []
        assert "transcription" not in out

    def test_no_meet_call_when_google_returned_no_conference(self, operator, recorder) -> None:
        recorder.conference = False
        out = _create(attendees=[GUEST])

        assert recorder.of("meet") == []
        assert "transcription" not in out

    def test_the_flag_turns_it_off(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_CALENDAR_MEET_ARTIFACTS", "0")
        out = _create(attendees=[GUEST])

        assert recorder.of("meet") == []
        assert "transcription" not in out


class TestTheFlag:
    def test_it_defaults_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine.feature_flags import calendar_meet_artifacts_enabled

        monkeypatch.delenv("ROBOTHOR_CALENDAR_MEET_ARTIFACTS", raising=False)
        assert calendar_meet_artifacts_enabled() is True

    def test_it_is_declared_in_the_settings_model(self) -> None:
        from robothor.settings.model import FlagSettings

        assert "calendar_meet_artifacts" in FlagSettings.model_fields


# ── The schema tells the model ────────────────────────────────────────


class TestTheSchemaSaysWhoOrganizes:
    def test_create_describes_the_guest_rule(self) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas

        fn = get_engine_schemas()["gws_calendar_create"]["function"]
        blob = fn["description"] + fn["parameters"]["properties"]["calendar"]["description"]
        low = blob.lower()
        assert "organiz" in low
        assert "guest" in low
        assert "calendar='operator'" in blob or 'calendar="operator"' in blob
        assert "transcript" in low
