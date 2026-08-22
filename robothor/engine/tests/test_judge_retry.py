"""A judge that returns nothing is a grader outage, not an agent failure.

Measured 2026-08-22: `_judge_output` made ONE attempt. When the model returned
an empty completion the case scored 0 and the agent wore it. That cost two
cases across two agents in a single evening -- agent-architect
`structural-detection` and curiosity-engine `dedup-prior-findings` -- both
recorded as "judge returned an empty completion".

An empty completion is transient. Retrying is the difference between grading
the agent and grading the weather.

The judge must still FAIL LOUDLY when it genuinely cannot grade: the previous
behaviour of returning 0.5 on any exception meant a rate-limited judge looked
like a mediocre agent, which is why every failure path returns
`JudgeOutcome(score=None, error=...)`. Retrying must not soften that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.handlers.benchmark import _judge_output


def _response(content: str | None):
    """Minimal litellm response shape."""
    msg = type("Msg", (), {"content": content})()
    choice = type("Choice", (), {"message": msg})()
    return type("Resp", (), {"choices": [choice]})()


class TestEmptyCompletionIsRetried:
    @pytest.mark.asyncio
    async def test_a_transient_empty_completion_recovers(self) -> None:
        """Empty once, valid on retry -> a real grade, not an error."""
        calls = [_response(None), _response('{"scores": [1, 1]}')]
        with patch("litellm.acompletion", new=AsyncMock(side_effect=calls)) as mock:
            outcome = await _judge_output("out", ["a", "b"], "m")
        assert mock.await_count == 2, "an empty completion must be retried"
        assert outcome.error is None, f"recovered call still reported {outcome.error!r}"
        assert outcome.score == 1.0

    @pytest.mark.asyncio
    async def test_a_transient_exception_recovers(self) -> None:
        with patch(
            "litellm.acompletion",
            new=AsyncMock(
                side_effect=[RuntimeError("429 rate limit"), _response('{"scores": [1]}')]
            ),
        ) as mock:
            outcome = await _judge_output("out", ["a"], "m")
        assert mock.await_count == 2
        assert outcome.error is None
        assert outcome.score == 1.0

    @pytest.mark.asyncio
    async def test_persistent_emptiness_still_errors(self) -> None:
        """Retrying must not paper over a judge that truly cannot grade."""
        with patch("litellm.acompletion", new=AsyncMock(return_value=_response(None))) as mock:
            outcome = await _judge_output("out", ["a"], "m")
        assert mock.await_count > 1, "should have retried"
        assert outcome.score is None
        assert outcome.error and "empty completion" in outcome.error

    @pytest.mark.asyncio
    async def test_a_good_first_call_is_not_retried(self) -> None:
        """No extra cost or latency on the happy path."""
        with patch(
            "litellm.acompletion", new=AsyncMock(return_value=_response('{"scores": [1, 0]}'))
        ) as mock:
            outcome = await _judge_output("out", ["a", "b"], "m")
        assert mock.await_count == 1
        assert outcome.score == 0.5


class TestDeterministicErrorsAreNotRetried:
    """Retrying a judgment the model actually made just burns tokens."""

    @pytest.mark.asyncio
    async def test_rubric_count_mismatch_is_not_retried(self) -> None:
        with patch(
            "litellm.acompletion",
            new=AsyncMock(return_value=_response('{"scores": [1, 1, 1]}')),
        ) as mock:
            outcome = await _judge_output("out", ["a", "b"], "m")
        assert mock.await_count == 1, "a count mismatch is the model's answer, not a blip"
        assert outcome.score is None
        assert outcome.error and "3 scores for 2" in outcome.error

    @pytest.mark.asyncio
    async def test_empty_rubric_is_not_retried(self) -> None:
        with patch("litellm.acompletion", new=AsyncMock()) as mock:
            outcome = await _judge_output("out", [], "m")
        assert mock.await_count == 0
        assert outcome.error and "empty rubric" in outcome.error
