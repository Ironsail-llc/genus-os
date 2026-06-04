"""Tests for Buddy session_goal_alignment dimension.

When an agent has an active session goal (objective + success_criteria
populated), Buddy reviews include a session_goal_alignment dimension
(0.0-1.0). The score is persisted as a second agent_reviews row with
dimension='session_goal_alignment' and feeds compute_goal_metrics as
session_goal_alignment_score.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.buddy_critic import (
    _REVIEW_PROMPT,
    Evidence,
    _extract_json,
)


def _evidence_with_goal() -> Evidence:
    return Evidence(
        run_id="run-1",
        agent_id="main",
        status="ok",
        started_at=datetime.now(UTC),
        duration_ms=1234,
        total_cost_usd=0.01,
        output_text_truncated="did the work",
        error_message=None,
        error_steps=[],
        tool_call_count=2,
        tool_error_count=0,
        session_goal_objective="Ship the merge feature",
        session_goal_criteria=["unified read path", "buddy alignment"],
    )


def test_evidence_dataclass_supports_session_goal_fields():
    ev = _evidence_with_goal()
    assert ev.session_goal_objective == "Ship the merge feature"
    assert ev.session_goal_criteria == ["unified read path", "buddy alignment"]
    d = ev.to_dict()
    assert d["session_goal"]["objective"] == "Ship the merge feature"
    assert d["session_goal"]["criteria"] == ["unified read path", "buddy alignment"]


def test_evidence_to_dict_omits_session_goal_when_not_set():
    ev = Evidence(
        run_id="run-2",
        agent_id="email-classifier",
        status="ok",
        started_at=None,
        duration_ms=None,
        total_cost_usd=None,
        output_text_truncated="",
        error_message=None,
    )
    d = ev.to_dict()
    assert "session_goal" not in d


def test_review_prompt_template_documents_alignment_dimension():
    # Loose check: the review prompt must reference the alignment dimension
    # so the LLM knows to optionally include it.
    assert "session_goal_alignment" in _REVIEW_PROMPT


@pytest.mark.asyncio
async def test_review_run_parses_alignment_from_llm_output(monkeypatch):
    from robothor.engine import buddy_critic as bc

    async def fake_llm_call(*, messages, model, temperature, max_tokens, timeout):
        # Mock LLM that returns a review including session_goal_alignment.
        class _Msg:
            content = (
                '{"rating": 4, "dimension": "quality", '
                '"specific_issue": "drifted from objective on this run", '
                '"suggested_action": "stay focused on the objective", '
                '"session_goal_alignment": 0.6, '
                '"session_goal_alignment_reason": "addresses 1 of 2 criteria"}'
            )

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    fake_module = MagicMock()
    fake_module.llm_call = fake_llm_call
    monkeypatch.setattr("robothor.engine.llm_client.llm_call", fake_llm_call, raising=False)

    review = await bc.review_run(
        _evidence_with_goal(),
        model="test-model",
        timeout_s=5,
    )
    assert review is not None
    assert review.session_goal_alignment == pytest.approx(0.6, abs=0.01)
    assert "criteria" in (review.session_goal_alignment_reason or "")


@pytest.mark.asyncio
async def test_review_run_clamps_alignment_to_0_1(monkeypatch):
    from robothor.engine import buddy_critic as bc

    async def fake_llm_call(**_):
        class _Msg:
            content = (
                '{"rating": 5, "dimension": "quality", '
                '"specific_issue": "x", "suggested_action": "y", '
                '"session_goal_alignment": 1.7}'
            )

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    monkeypatch.setattr("robothor.engine.llm_client.llm_call", fake_llm_call, raising=False)
    review = await bc.review_run(_evidence_with_goal(), model="x", timeout_s=5)
    assert review is not None
    assert 0.0 <= review.session_goal_alignment <= 1.0


@pytest.mark.asyncio
async def test_review_run_omits_alignment_when_no_goal_objective(monkeypatch):
    """If the evidence has no session goal, the LLM output's alignment
    field is ignored — reviews of agents without active goals don't
    pollute agent_reviews with synthetic alignment rows."""
    from robothor.engine import buddy_critic as bc

    async def fake_llm_call(**_):
        class _Msg:
            content = (
                '{"rating": 3, "dimension": "correctness", '
                '"specific_issue": "x", "suggested_action": "y", '
                '"session_goal_alignment": 0.9}'
            )

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    ev = Evidence(
        run_id="r",
        agent_id="a",
        status="ok",
        started_at=None,
        duration_ms=None,
        total_cost_usd=None,
        output_text_truncated="",
        error_message=None,
    )
    monkeypatch.setattr("robothor.engine.llm_client.llm_call", fake_llm_call, raising=False)
    review = await bc.review_run(ev, model="x", timeout_s=5)
    assert review is not None
    assert review.session_goal_alignment is None


def test_persist_review_writes_alignment_row_when_present():
    from robothor.engine import buddy_critic as bc

    review = bc.Review(
        agent_id="main",
        run_id="run-9",
        rating=4,
        dimension="quality",
        specific_issue="ok",
        suggested_action="keep going",
        raw_evidence=_evidence_with_goal(),
        session_goal_alignment=0.6,
        session_goal_alignment_reason="addresses 1 of 2 criteria",
    )

    with patch("robothor.crm.dal.create_review") as mock_create:
        mock_create.return_value = "rev-123"
        bc.persist_review(review, tenant_id="default")
        # Two reviews persisted: the dimension review + the alignment row.
        assert mock_create.call_count == 2
        kinds = {
            call.kwargs.get("categories", {}).get("dimension")
            for call in mock_create.call_args_list
        }
        assert "quality" in kinds
        assert "session_goal_alignment" in kinds


def test_persist_review_writes_one_row_when_no_alignment():
    from robothor.engine import buddy_critic as bc

    review = bc.Review(
        agent_id="main",
        run_id="run-10",
        rating=4,
        dimension="quality",
        specific_issue="ok",
        suggested_action="keep going",
        raw_evidence=_evidence_with_goal(),
    )

    with patch("robothor.crm.dal.create_review") as mock_create:
        mock_create.return_value = "rev-x"
        bc.persist_review(review, tenant_id="default")
        assert mock_create.call_count == 1


def test_get_session_goal_alignment_score_averages_recent_reviews():
    from robothor.engine.goals import _get_session_goal_alignment_score

    with patch("robothor.crm.dal.get_connection") as mock_get_conn:
        cur = MagicMock()
        # Three alignment ratings: 5, 4, 3 (out of 5). Avg=4.0 → (4-1)/4 = 0.75
        cur.fetchone.return_value = (4.0,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        score = _get_session_goal_alignment_score(
            agent_id="main",
            window_days=7,
            tenant_id="default",
        )
        assert score == pytest.approx(0.75, abs=0.01)


def test_get_session_goal_alignment_score_returns_none_when_no_reviews():
    from robothor.engine.goals import _get_session_goal_alignment_score

    with patch("robothor.crm.dal.get_connection") as mock_get_conn:
        cur = MagicMock()
        cur.fetchone.return_value = (None,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        score = _get_session_goal_alignment_score(agent_id="main", tenant_id="default")
        assert score is None


def test_extract_json_still_works_with_alignment_field():
    payload = (
        '{"rating": 4, "dimension": "quality", "specific_issue": "x", '
        '"suggested_action": "y", "session_goal_alignment": 0.7}'
    )
    parsed = _extract_json(payload)
    assert parsed is not None
    assert parsed["session_goal_alignment"] == 0.7
