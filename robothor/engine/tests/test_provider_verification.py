"""Google read-backs stay byte-identical; Microsoft-shaped results flow through.

* **Google characterization.** The read-back a Google instance makes after a
  ``gws_gmail_send`` / ``gws_gmail_reply`` / ``gws_calendar_create`` is the gws
  CLI, with an exact argv and a 5-second budget. These tests pin that argv
  byte for byte, so the provider branch (the Microsoft 365 read-back through
  Graph, covered by the workspace contract suite) cannot change what a live
  Google instance runs. One deliberate change is pinned too: an event the
  create put on the OPERATOR's calendar is read back there, not from
  ``primary`` (the assistant's own calendar).
* **Readers.** ``SendResult`` (``id``/``threadId``) and event ids from Microsoft
  365 are Google-shaped by design, so run verification, the recent-actions log,
  goal evidence and chat receipts take them unchanged.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools import verification
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.verification import reset_verification_budget, verify_tool_result

OWNER = "owner@example.com"
#: Graph immutable ids, the shape a Microsoft 365 send or create returns.
GRAPH_ID = "AAMkAGI2THVSAAA="
GRAPH_EVENT = "AAMkAGI2TGuLAAA="


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_verification_budget()
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "observe")
    monkeypatch.delenv("ROBOTHOR_WORKSPACE_PROVIDER", raising=False)
    from robothor.settings import reset_settings
    from robothor.workspace import reset_workspace_cache

    reset_settings()
    reset_workspace_cache()
    yield
    reset_settings()
    reset_workspace_cache()


@pytest.fixture
def evidence() -> Any:
    with patch.object(verification, "_insert_evidence") as mock:
        yield mock


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id="main", run_id="11111111-1111-1111-1111-111111111111", tenant_id="default"
    )


def _rows(mock: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in mock.call_args_list]


class _RecordingGws:
    """``gws._run_gws``, recorded: (argv, timeout) per call."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[tuple[list[str], Any]] = []

    def __call__(self, argv: list[str], timeout: Any = None) -> dict[str, Any]:
        self.calls.append((list(argv), timeout))
        return self.answer


@pytest.fixture
def gws_cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    from robothor.engine.tools.handlers import gws

    def install(answer: dict[str, Any]) -> _RecordingGws:
        fake = _RecordingGws(answer)
        monkeypatch.setattr(gws, "_run_gws", fake)
        return fake

    return install


# ── Google: the exact read-back argv (characterization) ─────────────────────


class TestGoogleReadBackIsUnchanged:
    @pytest.mark.parametrize("tool", ["gws_gmail_send", "gws_gmail_reply"])
    async def test_mail_read_back_argv(self, tool: str, gws_cli: Any, evidence: Any) -> None:
        cli = gws_cli({"id": "18f00000000000a1"})
        await verify_tool_result(tool, {}, {"id": "18f00000000000a1", "threadId": "t"}, _ctx())

        assert cli.calls == [
            (
                [
                    "gmail",
                    "users",
                    "messages",
                    "get",
                    "--params",
                    '{"userId": "me", "id": "18f00000000000a1", "format": "minimal"}',
                ],
                5,
            )
        ]
        assert _rows(evidence)[0]["verified"] is True
        assert _rows(evidence)[0]["reference"] == "gmail:18f00000000000a1"

    async def test_calendar_read_back_with_an_explicit_calendar_id(
        self, gws_cli: Any, evidence: Any
    ) -> None:
        cli = gws_cli({"id": "evt-1", "status": "confirmed"})
        await verify_tool_result(
            "gws_calendar_create",
            {"calendar_id": "team@group.calendar.example"},
            {"id": "evt-1", "calendar": {"kind": "other", "id": "team@group.calendar.example"}},
            _ctx(),
        )
        assert cli.calls == [
            (
                [
                    "calendar",
                    "events",
                    "get",
                    "--params",
                    '{"calendarId": "team@group.calendar.example", "eventId": "evt-1"}',
                ],
                5,
            )
        ]
        assert _rows(evidence)[0]["verified"] is True

    async def test_calendar_read_back_on_the_assistants_own_calendar(
        self, gws_cli: Any, evidence: Any
    ) -> None:
        cli = gws_cli({"id": "evt-2", "status": "confirmed"})
        await verify_tool_result(
            "gws_calendar_create",
            {"calendar": "own"},
            {"id": "evt-2", "calendar": {"kind": "own", "id": "primary"}},
            _ctx(),
        )
        assert cli.calls[0][0][-1] == '{"calendarId": "primary", "eventId": "evt-2"}'

    async def test_calendar_read_back_with_no_calendar_anywhere_is_primary(
        self, gws_cli: Any, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.tools.handlers import gws

        monkeypatch.setattr(gws, "_operator_calendar_address", lambda: "")
        cli = gws_cli({"id": "evt-3"})
        await verify_tool_result("gws_calendar_create", {}, {"id": "evt-3"}, _ctx())
        assert cli.calls[0][0][-1] == '{"calendarId": "primary", "eventId": "evt-3"}'

    async def test_a_cancelled_event_still_fails(self, gws_cli: Any, evidence: Any) -> None:
        gws_cli({"id": "evt-4", "status": "cancelled"})
        await verify_tool_result("gws_calendar_create", {}, {"id": "evt-4"}, _ctx())
        assert _rows(evidence)[0]["verified"] is False


class TestGoogleReadsTheCalendarTheWriteUsed:
    """The deliberate fix: an event on the OPERATOR's calendar is read back there."""

    async def test_operator_calendar_create_is_read_back_from_the_operator_calendar(
        self, gws_cli: Any, evidence: Any
    ) -> None:
        cli = gws_cli({"id": "evt-5", "status": "confirmed"})
        await verify_tool_result(
            "gws_calendar_create",
            {},
            {"id": "evt-5", "calendar": {"kind": "operator", "id": OWNER}},
            _ctx(),
        )
        assert cli.calls[0][0][-1] == json.dumps({"calendarId": OWNER, "eventId": "evt-5"})
        assert _rows(evidence)[0]["verified"] is True


# ── Readers of Microsoft-shaped results ─────────────────────────────────────


class TestMicrosoftShapedResultsFlowThroughTheReaders:
    """SendResult (id/threadId) and NormalizedEvent ids are Google-shaped by
    design, so every reader keyed on the tool families takes them unchanged."""

    def test_run_verification_supports_a_graph_send_claim(self) -> None:
        from robothor.engine.run_verification import verify_run

        steps = [
            {
                "step_number": 1,
                "step_type": "tool_call",
                "tool_name": "gws_gmail_send",
                "tool_input": {"to": "alice@example.com", "subject": "Hi", "body": "x"},
                "tool_output": {"id": GRAPH_ID, "threadId": "AAQkAGI2conv=", "labelIds": ["SENT"]},
            }
        ]
        verdict = verify_run("I sent the email to Alice.", steps)
        assert verdict.status == "verified"

    def test_run_verification_supports_a_graph_calendar_claim(self) -> None:
        from robothor.engine.run_verification import verify_run

        steps = [
            {
                "step_number": 1,
                "step_type": "tool_call",
                "tool_name": "gws_calendar_create",
                "tool_input": {"summary": "Sync", "start": "x", "end": "y"},
                "tool_output": {"id": GRAPH_EVENT, "calendar": {"kind": "operator", "id": OWNER}},
            }
        ]
        verdict = verify_run("I added the meeting to your calendar.", steps)
        assert verdict.status == "verified"

    def test_recent_actions_summarizes_a_graph_send_with_its_conversation(self) -> None:
        from types import SimpleNamespace

        from robothor.engine.recent_actions import summarize_run

        step = SimpleNamespace(
            tool_name="gws_gmail_send",
            tool_input={"to": "alice@example.com", "subject": "Hi"},
            tool_output={"id": GRAPH_ID, "threadId": "AAQkAGI2conv="},
        )
        run = SimpleNamespace(agent_id="main", trigger_type="telegram", steps=[step])
        out = summarize_run(run)
        assert "thread=AAQkAGI2conv=" in out

    def test_recent_actions_summarizes_a_graph_calendar_create(self) -> None:
        from types import SimpleNamespace

        from robothor.engine.recent_actions import summarize_run

        step = SimpleNamespace(
            tool_name="gws_calendar_create",
            tool_input={"summary": "Sync", "start": "2026-10-08T10:00:00Z"},
            tool_output={"id": GRAPH_EVENT},
        )
        run = SimpleNamespace(agent_id="main", trigger_type="telegram", steps=[step])
        assert "calendar_create: 'Sync'" in summarize_run(run)

    def test_goal_evidence_accepts_a_verified_microsoft_calendar_effect(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock
        from uuid import uuid4

        from robothor.goals.evidence import verify

        cur = MagicMock()
        cur.fetchone.return_value = {
            "tool_name": "gws_calendar_update",
            "state": "finished",
            "resolution": {
                "source": "tool_response",
                "result": {"id": GRAPH_EVENT, "status": "updated", "verification": "verified"},
            },
            "updated_at": datetime.now(UTC),
        }
        goal = {"created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
        out = verify(cur, "tenant", goal, "calendar-effect:" + str(uuid4()))
        assert out["independent"] is True

    def test_chat_receipts_read_a_microsoft_calendar_operation(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from uuid import uuid4

        from robothor.engine.chat_receipts import calendar_receipts

        op = str(uuid4())
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [
                {
                    "tool_name": "gws_calendar_add_attendees",
                    "tool_input": {"event_id": GRAPH_EVENT},
                    "tool_output": {"operation_id": op, "id": GRAPH_EVENT},
                }
            ],
            [
                {
                    "id": op,
                    "status": "completed",
                    "result": {
                        "verification": "verified",
                        "attendees_present": ["alice@example.com"],
                        "invitations_requested": True,
                    },
                }
            ],
        ]
        auth = SimpleNamespace(tenant_id="tenant", user_id="user")
        receipts = calendar_receipts(cur, {"id": "run", "agent_id": "main"}, auth)
        assert receipts[0]["verified"] is True
        assert receipts[0]["attendees_present"] == ["alice@example.com"]
