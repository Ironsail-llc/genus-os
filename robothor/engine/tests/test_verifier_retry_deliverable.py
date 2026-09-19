"""The verification retry must produce the deliverable, not a reply to the critic.

Live failure (2026-09-14): a scheduled briefing listed open decisions, the
default criteria judged that "not completed", the retry feedback said "address
these issues and try again", and the model answered the feedback — a 962-char
rebuttal — which is what the operator received. The real briefing sat in a note.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.verifier import (
    DEFAULT_CRITERIA,
    VerificationResult,
    format_verification_feedback,
)


def test_feedback_demands_the_deliverable_not_a_reply():
    text = format_verification_feedback(
        VerificationResult(passed=False, issues=["Open items remain"])
    )
    lowered = text.lower()
    assert "complete deliverable" in lowered
    assert "do not reply to this feedback" in lowered
    assert "try again" not in lowered


def test_default_criteria_treat_reported_open_items_as_content():
    lowered = DEFAULT_CRITERIA.lower()
    assert "open items" in lowered
    assert "not failures" in lowered


@pytest.mark.asyncio
async def test_retry_that_yields_nothing_keeps_the_original_output(monkeypatch):
    """A retry that produces no text must not turn a delivered report into nothing."""
    from robothor.engine import run_lifecycle, verifier

    monkeypatch.setattr(
        verifier,
        "verify_output",
        AsyncMock(return_value=VerificationResult(passed=False, issues=["x"])),
    )
    lifecycle = run_lifecycle.RunLifecycleMixin.__new__(run_lifecycle.RunLifecycleMixin)
    lifecycle._run_loop = AsyncMock(return_value=None)
    session = MagicMock()
    session.messages = []
    session.run.steps = []
    session.get_final_text.return_value = None
    config = MagicMock()
    config.verification_prompt = None
    config.provider_order = {}

    out = await run_lifecycle.RunLifecycleMixin._run_verification(
        lifecycle, config, session, ["m"], [], "the report", None, None
    )

    assert out == "the report"
    assert session.messages[0]["content"].startswith("[VERIFICATION FAILED]")
