"""Phase 4 — POST /api/tasks/{task_id}/answer.

Distinct from approve/reject: an answer carries free-text content, may
optionally advance the task status (subject to VALID_TRANSITIONS), and
fires a `question_answered` notification rather than `review_approved`
or `review_rejected`. Backs the Helm Task Board's typed-answer UI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.mark.asyncio
async def test_answer_endpoint_returns_success_when_dal_writes(test_client):
    """Happy path: dal.answer_question returns True → 200 + success body."""
    with patch("routers.notes_tasks.answer_question", return_value=True) as mock_ans:
        with patch("routers.notes_tasks.publish") as mock_pub:
            r = await test_client.post(
                "/api/tasks/abc-123/answer",
                json={"answer": "Yes, drop the vendor", "advanceTo": "IN_PROGRESS"},
                headers={"X-Agent-Id": "helm-user"},
            )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["id"] == "abc-123"

    call_kwargs = mock_ans.call_args.kwargs
    assert call_kwargs["task_id"] == "abc-123"
    assert call_kwargs["answer"] == "Yes, drop the vendor"
    assert call_kwargs["advance_to"] == "IN_PROGRESS"
    assert call_kwargs["by"] == "helm-user"

    # task.answered event published on the agent stream
    mock_pub.assert_called_once()
    assert mock_pub.call_args.args[0] == "agent"
    assert mock_pub.call_args.args[1] == "task.answered"
    payload = mock_pub.call_args.args[2]
    assert payload["task_id"] == "abc-123"
    assert payload["by"] == "helm-user"


@pytest.mark.asyncio
async def test_answer_endpoint_defaults_by_to_helm_user(test_client):
    """No X-Agent-Id → answerer attributed to "helm-user"."""
    with patch("routers.notes_tasks.answer_question", return_value=True) as mock_ans:
        with patch("routers.notes_tasks.publish"):
            await test_client.post(
                "/api/tasks/t1/answer",
                json={"answer": "Proceed"},
            )

    assert mock_ans.call_args.kwargs["by"] == "helm-user"


@pytest.mark.asyncio
async def test_answer_endpoint_rejects_empty_answer(test_client):
    """An empty/whitespace answer is a validation error."""
    with patch("routers.notes_tasks.answer_question") as mock_ans:
        r = await test_client.post(
            "/api/tasks/t1/answer",
            json={"answer": "   "},
        )

    assert r.status_code == 422
    assert "error" in r.json()
    mock_ans.assert_not_called()


@pytest.mark.asyncio
async def test_answer_endpoint_returns_404_when_dal_returns_false(test_client):
    """Task not found OR invalid transition → 404."""
    with patch("routers.notes_tasks.answer_question", return_value=False):
        r = await test_client.post(
            "/api/tasks/missing/answer",
            json={"answer": "x"},
        )

    assert r.status_code == 404
    assert "error" in r.json()


@pytest.mark.asyncio
async def test_answer_endpoint_passes_channel_through(test_client):
    """The channel field reaches the DAL — defaults to 'helm' but can be overridden."""
    with patch("routers.notes_tasks.answer_question", return_value=True) as mock_ans:
        with patch("routers.notes_tasks.publish"):
            await test_client.post(
                "/api/tasks/t1/answer",
                json={"answer": "yes", "channel": "telegram"},
            )

    assert mock_ans.call_args.kwargs["channel"] == "telegram"
