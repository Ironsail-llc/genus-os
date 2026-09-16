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
