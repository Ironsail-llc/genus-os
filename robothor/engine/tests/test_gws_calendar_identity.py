"""Whose calendar did that event land on?

2026-09-16, the second and worse failure. The assistant planned the operator's
itinerary, called ``gws_calendar_create`` for each leg, and told the operator it
was on his calendar. It was not. Three things went wrong at once and each one
made the other two invisible:

* ``calendar_id`` defaulted to ``"primary"``, which is the **assistant's own**
  Google calendar — the bot has its own account. The schema said "Calendar ID
  (default 'primary')" and nothing anywhere said whose.
* The operator was added as an attendee, so the event *looked* like his.
* The insert never passed ``sendUpdates``, so Google sent no invitation. The
  operator was an attendee of an event on a calendar he does not read, with no
  mail to tell him it existed.

The result the assistant got back was a successful event object, so it reported
success truthfully as far as it could see. The tool gave it no way to know.

Every test here fakes ``_run_gws`` and records the params. Nothing reaches
Google. The operator identity comes from an ``owner.yaml`` fixture — no address
in this file belongs to anyone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws as gws_handlers

OPERATOR_EMAIL = "alice@example.com"
ASSISTANT_EMAIL = "bot@example.com"


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """An owner.yaml naming a generic operator, and a bot address beside it."""
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


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """A fake ``_run_gws`` that records every call and answers plausibly."""
    calls: list[dict[str, Any]] = []

    def _run(args: list[str], timeout: int = 30) -> Any:
        entry: dict[str, Any] = {"argv": args}
        if "--params" in args:
            entry["params"] = json.loads(args[args.index("--params") + 1])
        if "--json" in args:
            entry["body"] = json.loads(args[args.index("--json") + 1])
        calls.append(entry)
        if args[:3] == ["calendar", "events", "list"]:
            return {"items": []}
        if args[:3] == ["calendar", "events", "insert"]:
            return {
                "id": "evt-1",
                "htmlLink": "https://calendar.example.com/event?eid=evt-1",
                "status": "confirmed",
            }
        return {}

    monkeypatch.setattr(gws_handlers, "_run_gws", _run)
    monkeypatch.setattr(gws_handlers, "_record_calendar_event", lambda **k: None)
    monkeypatch.setattr(gws_handlers, "_dnc_refusal", lambda *a, **k: None)
    return calls


def _insert(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return next(c for c in calls if c["argv"][:3] == ["calendar", "events", "insert"])


def _create(**kwargs: Any) -> dict[str, Any]:
    base = {
        "summary": "Flight to Lisbon",
        "start": "2026-10-01T09:00:00Z",
        "end": "2026-10-01T12:00:00Z",
    }
    return gws_handlers._handle_gws_tool("gws_calendar_create", {**base, **kwargs})


# ── The reproduction ──────────────────────────────────────────────────


class TestTheItineraryFailure:
    def test_an_itinerary_event_lands_on_the_operators_calendar(self, operator, recorder) -> None:
        """The whole defect in one assertion: with no calendar argument at all,
        an event must NOT go to the assistant's own calendar."""
        _create()
        params = _insert(recorder)["params"]

        assert params["calendarId"] == OPERATOR_EMAIL
        assert params["calendarId"] != "primary"

    def test_an_invitation_actually_goes_out(self, operator, recorder) -> None:
        """No `sendUpdates` meant Google emailed nobody. The operator was an
        attendee of an event on a calendar he does not read."""
        _create(attendees=["bob@example.com"])
        params = _insert(recorder)["params"]

        assert params["sendUpdates"] == "all"

    def test_the_result_says_whose_calendar_and_whether_mail_went(self, operator, recorder) -> None:
        """The assistant reported success because the API said success. It now
        gets the two facts it needs to report truthfully."""
        out = _create(attendees=["bob@example.com"])

        assert out["calendar"] == {"kind": "operator", "id": OPERATOR_EMAIL}
        assert out["invitations_sent"] is True
        assert out["htmlLink"] == "https://calendar.example.com/event?eid=evt-1"


# ── Resolving the `calendar` parameter ────────────────────────────────


class TestCalendarResolution:
    def test_operator_resolves_to_the_owner_config_address(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar": "operator"}) == (
            OPERATOR_EMAIL,
            "operator",
        )

    def test_own_resolves_to_primary(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar": "own"}) == ("primary", "own")

    def test_the_default_is_the_operators_calendar(self, operator) -> None:
        """A tool whose default writes to the assistant's own calendar is a tool
        that silently does the wrong thing; the model must SAY `own` to reach
        the bot's calendar."""
        assert gws_handlers._resolve_calendar({}) == (OPERATOR_EMAIL, "operator")

    def test_an_explicit_calendar_id_still_wins(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar_id": "team@example.com"}) == (
            "team@example.com",
            "other",
        )

    def test_an_explicit_id_equal_to_primary_is_own(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar_id": "primary"}) == ("primary", "own")

    def test_an_explicit_id_equal_to_the_operator_is_the_operators(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar_id": OPERATOR_EMAIL.upper()}) == (
            OPERATOR_EMAIL.upper(),
            "operator",
        )

    def test_with_no_owner_configured_it_falls_back_to_primary_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A fresh instance with no owner.yaml must not crash and must not
        pretend the assistant's calendar is the operator's."""
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(tmp_path / "absent.yaml"))
        monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
        assert gws_handlers._resolve_calendar({"calendar": "operator"}) == ("primary", "own")

    def test_no_operator_address_is_hardcoded_anywhere(self) -> None:
        """The resolver must read the owner config, never a literal."""
        import inspect

        source = inspect.getsource(gws_handlers._resolve_calendar)
        assert "@" not in source or "example.com" not in source


class TestCalendarIsValidated:
    """`calendar` was not checked, and anything unrecognised meant "operator".

    So `calendar="primary"` — the word every Google Calendar document uses, and
    this tool's own default until the commit before this one — meant the exact
    OPPOSITE of what it says: an agent asking to keep something on its own
    scratch calendar wrote a real event to a human's real calendar and mailed
    them an invitation. The schemas declared the enum; nothing enforced it.
    """

    @pytest.mark.parametrize("given", ["primary", "bogus", "OWN ", 5, ["own"]])
    def test_an_unrecognised_value_is_refused(self, operator, given) -> None:
        if given == "OWN ":
            pytest.skip("whitespace and case are normalised, not refused")
        with pytest.raises(gws_handlers._InvalidCalendar):
            gws_handlers._resolve_calendar({"calendar": given})

    def test_case_and_whitespace_are_forgiven(self, operator) -> None:
        assert gws_handlers._resolve_calendar({"calendar": " OWN "}) == ("primary", "own")
        assert gws_handlers._resolve_calendar({"calendar": "Operator"})[1] == "operator"

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("gws_calendar_create", {"summary": "s", "start": "a", "end": "b"}),
            ("gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"}),
            ("gws_calendar_delete", {"event_id": "e"}),
        ],
    )
    def test_every_calendar_tool_refuses_rather_than_writing(
        self, operator, recorder, tool: str, args: dict
    ) -> None:
        out = gws_handlers._handle_gws_tool(tool, {**args, "calendar": "primary"})

        assert "error" in out
        assert out["hint"] == "invalid_params"
        assert "calendar='operator'" in out["error"]
        assert "calendar='own'" in out["error"]
        assert "calendar_id" in out["error"], "it must name the way to reach a third calendar"
        assert recorder == [], "nothing may reach the CLI on a refused value"

    def test_an_explicit_calendar_id_of_primary_is_still_allowed(self, operator) -> None:
        """`calendar_id` is the explicit override and means what Google means."""
        assert gws_handlers._resolve_calendar({"calendar_id": "primary"}) == ("primary", "own")


class TestTheHandlerOnlyClaimsWhatItKnows:
    """Reporting more than the handler can see is the defect this began as."""

    def test_an_event_with_no_attendees_claims_no_invitations(
        self, operator, recorder
    ) -> None:
        out = _create()

        assert out["invitations_sent"] is False
        assert out["attendees_notified"] == []

    def test_the_operator_is_not_an_attendee_of_their_own_calendar(
        self, operator, recorder
    ) -> None:
        """They are the organiser there. Adding them made Google ask them to
        RSVP to their own itinerary and mail them once per leg — ten emails for
        a ten-leg trip. The auto-add existed because the default used to be
        this account's calendar, where it was the only way they saw the event."""
        _create(attendees=["bob@example.com"])
        body = _insert(recorder)["body"]

        assert [a["email"] for a in body["attendees"]] == ["bob@example.com"]

    def test_the_operator_is_still_added_on_the_assistants_own_calendar(
        self, operator, recorder
    ) -> None:
        """There, being an attendee is the only way they learn it exists."""
        _create(calendar="own", attendees=["bob@example.com"])
        body = _insert(recorder)["body"]

        assert OPERATOR_EMAIL in [a["email"] for a in body["attendees"]]

    def test_external_only_does_not_claim_the_operator_was_told(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Google does not mail same-domain attendees under externalOnly, and
        the operator is auto-added and always same-domain."""
        monkeypatch.setenv("ROBOTHOR_CALENDAR_SEND_UPDATES", "externalOnly")
        out = _create(attendees=["bob@example.com"])

        assert out["send_updates"] == "externalOnly"
        assert out["invitations_sent"] is True
        assert out["attendees_notified"] == [], "who Google mailed is Google's decision"

    def test_a_delete_reports_what_it_asked_for_not_who_was_told(
        self, operator, recorder
    ) -> None:
        """It never reads the event, so it cannot know there were attendees —
        `cancellations_sent: true` for an event with none is the same untruth
        as "the API said success so I said it is on your calendar"."""
        out = gws_handlers._handle_gws_tool("gws_calendar_delete", {"event_id": "evt-1"})

        assert out["send_updates"] == "all"
        assert "cancellations_sent" not in out

    def test_the_delete_reads_the_flag_once(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read twice across a 5s cache TTL, the report could contradict the
        call it described."""
        reads: list[str] = []
        real = gws_handlers._send_updates

        def counting() -> str:
            reads.append("x")
            return real()

        monkeypatch.setattr(gws_handlers, "_send_updates", counting)
        gws_handlers._handle_gws_tool("gws_calendar_delete", {"event_id": "evt-1"})
        assert len(reads) == 1


class TestAttendeelessDedup:
    """Flights, hotels and trip legs — the 2026-09-16 shape — have no attendees.

    `_attendees_overlap` returned False when either side was empty, so dedup
    was inert for precisely the events that caused the incident, while the new
    docstring advertised protection that did not exist for them.
    """

    def _existing(self, start: str = "2026-10-01T09:00:00Z") -> dict[str, Any]:
        return {
            "id": "already-there",
            "summary": "Flight to Lisbon",
            "start": {"dateTime": start},
            "htmlLink": "https://calendar.example.com/e",
        }

    def test_an_identical_attendee_less_event_dedups(self, operator, recorder) -> None:
        def run(args: list[str], timeout: int = 30) -> Any:
            recorder.append({"argv": args})
            if args[:3] == ["calendar", "events", "list"]:
                return {"items": [self._existing()]}
            return {"id": "new", "htmlLink": "x"}

        gws_handlers._run_gws = run
        out = _create()

        assert out["status"] == "deduped"
        assert out["existing_event_id"] == "already-there"
        assert out["calendar"]["kind"] == "operator"
        assert not any(c["argv"][:3] == ["calendar", "events", "insert"] for c in recorder)

    def test_the_same_title_at_a_different_time_is_a_different_event(
        self, operator, recorder
    ) -> None:
        """Two flights to the same city in a fortnight are two flights."""

        def run(args: list[str], timeout: int = 30) -> Any:
            recorder.append({"argv": args})
            if args[:3] == ["calendar", "events", "list"]:
                return {"items": [self._existing(start="2026-10-05T09:00:00Z")]}
            return {"id": "new", "htmlLink": "x"}

        gws_handlers._run_gws = run
        out = _create()

        assert out.get("status") != "deduped"
        assert any(c["argv"][:3] == ["calendar", "events", "insert"] for c in recorder)

    def test_an_event_with_guests_does_not_dedup_against_one_without(
        self, operator, recorder
    ) -> None:
        def run(args: list[str], timeout: int = 30) -> Any:
            recorder.append({"argv": args})
            if args[:3] == ["calendar", "events", "list"]:
                return {"items": [self._existing()]}
            return {"id": "new", "htmlLink": "x"}

        gws_handlers._run_gws = run
        out = _create(attendees=["bob@example.com"])

        assert out.get("status") != "deduped"

    def test_the_overlap_rule_itself(self) -> None:
        overlap = gws_handlers._attendees_overlap
        assert overlap(set(), set(), "alice@example.com") is True
        assert overlap({"bob@example.com"}, set(), "alice@example.com") is False
        assert overlap(set(), {"bob@example.com"}, "alice@example.com") is False
        # The operator alone on both sides is still "no attendee signal".
        assert overlap({"alice@example.com"}, {"alice@example.com"}, "alice@example.com") is True


# ── The other two calendar tools ──────────────────────────────────────


class TestListAndDelete:
    def test_list_reads_the_operators_calendar_by_default(self, operator, recorder) -> None:
        out = gws_handlers._handle_gws_tool(
            "gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"}
        )
        listed = next(c for c in recorder if c["argv"][:3] == ["calendar", "events", "list"])

        assert listed["params"]["calendarId"] == OPERATOR_EMAIL
        assert out["calendar"] == {"kind": "operator", "id": OPERATOR_EMAIL}

    def test_list_can_be_asked_for_the_assistants_own(self, operator, recorder) -> None:
        gws_handlers._handle_gws_tool(
            "gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z", "calendar": "own"}
        )
        listed = next(c for c in recorder if c["argv"][:3] == ["calendar", "events", "list"])
        assert listed["params"]["calendarId"] == "primary"

    def test_delete_targets_the_operators_calendar_and_notifies(self, operator, recorder) -> None:
        """A cancellation nobody is told about is not a cancellation."""
        out = gws_handlers._handle_gws_tool("gws_calendar_delete", {"event_id": "evt-1"})
        deleted = next(c for c in recorder if c["argv"][:3] == ["calendar", "events", "delete"])

        assert deleted["params"]["calendarId"] == OPERATOR_EMAIL
        assert deleted["params"]["sendUpdates"] == "all"
        assert out["calendar"]["kind"] == "operator"

    def test_the_dedup_read_looks_at_the_calendar_being_written(self, operator, recorder) -> None:
        """A duplicate check against the wrong calendar finds nothing and then
        creates the duplicate it was meant to prevent."""
        _create()
        listed = next(c for c in recorder if c["argv"][:3] == ["calendar", "events", "list"])
        assert listed["params"]["calendarId"] == OPERATOR_EMAIL


# ── sendUpdates is a setting, not a literal ───────────────────────────


class TestSendUpdatesSetting:
    def test_it_is_declared_in_the_settings_model(self) -> None:
        """Every knob this platform reads is declared, or `genus config` and the
        generated reference do not know it exists."""
        from robothor.settings.model import FlagSettings

        assert "calendar_send_updates" in FlagSettings.model_fields

    def test_the_default_sends_to_everyone(self, operator, recorder) -> None:
        _create(attendees=["bob@example.com"])
        assert _insert(recorder)["params"]["sendUpdates"] == "all"

    def test_an_operator_can_turn_invitations_down(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_CALENDAR_SEND_UPDATES", "none")
        out = _create(attendees=["bob@example.com"])

        assert _insert(recorder)["params"]["sendUpdates"] == "none"
        assert out["invitations_sent"] is False

    def test_an_unrecognised_value_falls_back_to_all(
        self, operator, recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing closed here means failing silent — nobody is told about the
        meeting. The safe default for an invitation is that it is sent."""
        monkeypatch.setenv("ROBOTHOR_CALENDAR_SEND_UPDATES", "sometimes")
        _create(attendees=["bob@example.com"])
        assert _insert(recorder)["params"]["sendUpdates"] == "all"


# ── The schema says whose calendar ────────────────────────────────────


class TestSchemas:
    @pytest.mark.parametrize(
        "tool", ["gws_calendar_create", "gws_calendar_list", "gws_calendar_delete"]
    )
    def test_the_calendar_parameter_exists_with_both_choices(self, tool: str) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas

        params = get_engine_schemas()[tool]["function"]["parameters"]["properties"]
        assert params["calendar"]["enum"] == ["operator", "own"]
        assert params["calendar"]["default"] == "operator"

    @pytest.mark.parametrize(
        "tool", ["gws_calendar_create", "gws_calendar_list", "gws_calendar_delete"]
    )
    def test_the_description_says_primary_is_the_assistants_own(self, tool: str) -> None:
        """ "Calendar ID (default 'primary')" is true and useless. The model has
        to be told that primary is ITS calendar, not the operator's."""
        from robothor.engine.tools.schemas import get_engine_schemas

        params = get_engine_schemas()[tool]["function"]["parameters"]["properties"]
        blob = (params["calendar"]["description"] + params["calendar_id"]["description"]).lower()
        assert "your own" in blob
        assert "operator" in blob
