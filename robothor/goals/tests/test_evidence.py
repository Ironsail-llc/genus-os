from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from robothor.goals.evidence import verify


def test_calendar_receipt_must_exist_in_tenant_and_be_fresh():
    cur = MagicMock()
    now = datetime.now(UTC)
    goal = {
        "created_at": (now - timedelta(days=2)).isoformat(),
        "assessment": {"at": (now - timedelta(days=1)).isoformat()},
    }
    reference = "calendar-operation:" + str(uuid4())
    cur.fetchone.return_value = {
        "status": "completed",
        "result": {"verification": "verified"},
        "updated_at": now,
    }
    assert verify(cur, "tenant", goal, reference)["independent"] is True
    goal["success_criteria"] = ["Increase revenue", reference]
    assert verify(cur, "tenant", goal, reference, 0)["criterion_verified"] is False
    assert verify(cur, "tenant", goal, reference, 1)["criterion_verified"] is True
    assert cur.execute.call_args.args[1][0] == "tenant"
    cur.fetchone.return_value["updated_at"] = now - timedelta(days=2)
    with pytest.raises(ValueError, match="predates"):
        verify(cur, "tenant", goal, reference)
    cur.fetchone.return_value = None
    with pytest.raises(ValueError, match="tenant"):
        verify(cur, "tenant", goal, reference)


def test_arbitrary_reference_is_not_labeled_independent():
    cur = MagicMock()
    assert verify(cur, "tenant", {}, "receipt:invented")["independent"] is False
    cur.execute.assert_not_called()


def _effect_row(**changes):
    row = {
        "tool_name": "gws_calendar_update",
        "state": "finished",
        "resolution": {
            "source": "tool_response",
            "result": {"status": "updated", "verification": "verified"},
        },
        "updated_at": datetime.now(UTC),
    }
    row.update(changes)
    return row


def test_a_verified_calendar_edit_effect_is_independent_evidence():
    """Direct calendar edits record their result in the runtime effect ledger,
    not in the retired calendar_operations table."""
    cur = MagicMock()
    goal = {"created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    reference = "calendar-effect:" + str(uuid4())
    cur.fetchone.return_value = _effect_row()
    result = verify(cur, "tenant", goal, reference)
    assert result["independent"] is True
    assert result["method"] == "calendar_effect_receipt"
    assert cur.execute.call_args.args[1][0] == "tenant"


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_name": "create_task"},
        {"state": "uncertain"},
        {"resolution": {"result": {"status": "partial", "verification": "unverified"}}},
        {"resolution": {"result": {"verification": "verified", "error": "x"}}},
    ],
)
def test_an_unverified_or_foreign_effect_is_not_calendar_evidence(changes):
    cur = MagicMock()
    goal = {"created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    cur.fetchone.return_value = _effect_row(**changes)
    with pytest.raises(ValueError, match="calendar"):
        verify(cur, "tenant", goal, "calendar-effect:" + str(uuid4()))
