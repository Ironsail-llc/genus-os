"""Tool descriptions and the principals line name the right workspace.

The ``gws_*`` tools serve Google or Microsoft 365 (``workspace_provider``), so
their descriptions must not promise Gmail or Google Meet to an Outlook
instance -- and the line telling the model it is a separate principal must
say which kind of account it has. Names and parameters never change: the
guards are keyed on them.

The Google principals line is pinned verbatim (characterization): it is part
of every run's engine-context turn on the live Google instance.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import (
    CALENDAR_READ_TOOLS,
    CALENDAR_WRITE_TOOLS,
    MAIL_READ_TOOLS,
    MAIL_WRITE_TOOLS,
)


@pytest.fixture
def identities(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import yaml

    from robothor.settings import reset_settings

    owner = tmp_path / "owner.yaml"
    owner.write_text(
        yaml.safe_dump(
            {
                "tenant_id": "fixture",
                "first_name": "Alice",
                "last_name": "Example",
                "email": "alice@example.com",
            }
        )
    )
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", "bot@example.com")
    monkeypatch.delenv("ROBOTHOR_WORKSPACE_PROVIDER", raising=False)
    monkeypatch.delenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", raising=False)
    reset_settings()
    yield
    reset_settings()


def _set(monkeypatch: pytest.MonkeyPatch, **pairs: str) -> None:
    from robothor.settings import reset_settings

    for name, value in pairs.items():
        monkeypatch.setenv(name, value)
    reset_settings()


class TestPrincipalsNote:
    def test_google_wording_is_unchanged(self, identities: None) -> None:
        from robothor.engine.toolset_prep import principals_note

        assert principals_note() == (
            "You are a separate principal with your own Google account (bot@example.com); "
            "the operator is Alice Example <alice@example.com>. When the operator says "
            "'my calendar', 'my email' or 'my files' they mean theirs, not yours — work is "
            "only done for them when it landed in their account."
        )

    def test_microsoft365_names_a_microsoft_account(
        self, identities: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.toolset_prep import principals_note

        _set(monkeypatch, ROBOTHOR_WORKSPACE_PROVIDER="microsoft365")
        note = principals_note()
        assert note.startswith(
            "You are a separate principal with your own Microsoft 365 account (bot@example.com)"
        )
        assert "Google" not in note
        assert "alice@example.com" in note

    def test_microsoft365_falls_back_to_the_assistant_mailbox(
        self, identities: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.toolset_prep import principals_note

        _set(
            monkeypatch,
            ROBOTHOR_WORKSPACE_PROVIDER="microsoft365",
            ROBOTHOR_AI_EMAIL="",
            ROBOTHOR_M365_ASSISTANT_MAILBOX="assistant@example.com",
        )
        assert "Microsoft 365 account (assistant@example.com)" in principals_note()


def _schemas() -> dict:
    from robothor.engine.tools.schemas import get_engine_schemas

    return get_engine_schemas()


WORKSPACE_MAIL_AND_CALENDAR = sorted(
    MAIL_READ_TOOLS | MAIL_WRITE_TOOLS | CALENDAR_READ_TOOLS | CALENDAR_WRITE_TOOLS
)


class TestToolDescriptionsAreProviderNeutral:
    @pytest.mark.parametrize("tool", WORKSPACE_MAIL_AND_CALENDAR)
    def test_no_google_only_promise(self, tool: str) -> None:
        import json

        text = json.dumps(_schemas()[tool]["function"])
        for phrase in (
            "Gmail message ID",
            "Gmail thread ID",
            "Google Meet video",
            "Gmail search query",
        ):
            assert phrase not in text, f"{tool} still promises {phrase!r}"

    def test_with_meet_names_both_online_meetings(self) -> None:
        prop = _schemas()["gws_calendar_create"]["function"]["parameters"]["properties"]
        assert "Google Meet" in prop["with_meet"]["description"]
        assert "Teams" in prop["with_meet"]["description"]

    def test_names_and_parameters_are_unchanged(self) -> None:
        """The guards and every manifest are keyed on these; only words moved."""
        schemas = _schemas()
        params = {
            tool: sorted(schemas[tool]["function"]["parameters"]["properties"])
            for tool in ("gws_gmail_search", "gws_gmail_get", "gws_gmail_reply", "gws_gmail_send")
        }
        assert params == {
            "gws_gmail_search": ["max_results", "query"],
            "gws_gmail_get": ["format", "max_chars", "message_id", "thread_id"],
            "gws_gmail_reply": ["body", "cc", "thread_id"],
            "gws_gmail_send": [
                "body",
                "cc",
                "content_type",
                "in_reply_to",
                "subject",
                "thread_id",
                "to",
            ],
        }
        create = schemas["gws_calendar_create"]["function"]["parameters"]
        assert sorted(create["properties"]) == [
            "attendee_confirmed",
            "attendees",
            "calendar",
            "calendar_id",
            "description",
            "end",
            "force",
            "location",
            "start",
            "summary",
            "with_meet",
        ]
        assert create["required"] == ["summary", "start", "end"]
