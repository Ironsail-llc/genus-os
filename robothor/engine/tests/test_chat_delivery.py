"""Initial delivery must not require another model to recover known action results."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from robothor.engine import chat_delivery
from robothor.engine.chat_result import receipt_result_text
from robothor.engine.models import RunStatus


def run(status=RunStatus.CANCELLED):
    return SimpleNamespace(
        id=str(uuid4()),
        status=status,
        output_text="False success",
        error_message="Deadline expired",
    )


@pytest.mark.parametrize("status", [RunStatus.CANCELLED, RunStatus.TIMEOUT, RunStatus.FAILED])
def test_interrupted_receipts_lead_without_claiming_whole_request_complete(status):
    receipt = {
        "kind": "runtime_effect",
        "tool_name": "create_task",
        "status": "confirmed",
        "verified": True,
        "operation_id": "one",
    }
    text = receipt_result_text(run(status), [receipt])
    assert text.startswith("The task was created.")
    assert "remaining work is not confirmed" in text
    assert "False success" not in text
    unresolved = {**receipt, "status": "uncertain", "verified": False}
    text = receipt_result_text(run(status), [receipt, unresolved])
    assert "outcome is still unresolved" in text


async def test_successful_chat_does_not_add_audit_latency(monkeypatch):
    read = Mock(side_effect=AssertionError("Unexpected audit lookup"))
    monkeypatch.setattr(chat_delivery, "_saved_result", read)
    assert (await chat_delivery.final_result(run(RunStatus.COMPLETED), None))[
        "text"
    ] == "False success"
    read.assert_not_called()


async def test_unavailable_audit_preserves_interrupted_status(monkeypatch):
    monkeypatch.setattr(chat_delivery, "_saved_result", Mock(side_effect=OSError("unavailable")))
    assert (await chat_delivery.final_result(run(), None))[
        "text"
    ] == "[Run cancelled: Deadline expired]"


async def test_audit_read_deadline_preserves_interrupted_status(monkeypatch):
    async def stalled(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_delivery.asyncio, "to_thread", stalled)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(chat_delivery.asyncio, "timeout", lambda _: real_timeout(0.01))
    assert (await chat_delivery.final_result(run(), None))[
        "text"
    ] == "[Run cancelled: Deadline expired]"


@pytest.mark.parametrize(
    "case", ["pending", "running", "different_run", "already_completed", "absent"]
)
def test_receipts_survive_terminal_row_write_race_without_reinterpreting_other_runs(
    monkeypatch, case
):
    observed = run(RunStatus.FAILED)
    auth = SimpleNamespace(tenant_id="tenant", user_id="operator")
    row = {
        "id": observed.id,
        "agent_id": "main",
        "status": "running",
        "output_text": None,
        "error_message": None,
    }
    if case == "pending":
        row["status"] = "pending"
    elif case == "already_completed":
        row["status"] = "completed"
    elif case == "different_run":
        observed.id = str(uuid4())
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None if case == "absent" else row
    monkeypatch.setattr(chat_delivery, "get_connection", lambda: connection)
    monkeypatch.setattr(chat_delivery, "family_calendar_receipts", lambda *args: [])
    monkeypatch.setattr(chat_delivery, "family_goal_receipts", lambda *args: [])
    read_receipts = Mock(
        return_value=[
            {
                "kind": "runtime_effect",
                "tool_name": "create_task",
                "status": "confirmed",
                "verified": True,
                "operation_id": "one",
            }
        ]
    )
    monkeypatch.setattr(chat_delivery, "family_effect_receipts", read_receipts)
    result = chat_delivery._saved_result(row["id"], auth, observed)
    assert cursor.execute.call_args.args[1] == (row["id"], "tenant", "operator")
    if case in {"pending", "running"}:
        assert result["status"] == "failed"
        assert result["text"].startswith("The task was created.")
        assert "remaining work is not confirmed" in result["text"]
        assert result["audit_outcome"]
    else:
        assert result is None
        read_receipts.assert_not_called()
