"""A budget-exhausted thinking turn is a fact about the run, not a mystery.

One measured sweep run ended with `reasoning_only (finish_reason=length,
output_tokens=8192, reasoning_tokens=8919)`: the model spent its entire output
ceiling on reasoning and returned nothing. The engine already self-heals that
on the non-streaming path — the same model, re-asked once with a reduced
thinking budget — and `test_reasoning_only_is_not_empty.py` covers the re-ask
itself.

What is asserted here is the other half, because a self-heal nobody can see is
indistinguishable from an outage: the outcome is classified as reasoning-only
rather than empty, it lands as an attempt row, and every counter that asks "did
this run go wrong" can separate it from a real error. That is the number the
bench's `usage.json` publishes as `llm_attempts_failed`.

The gap this does NOT close: the streaming path has no re-ask (llm_client
`_call_llm_streaming` advances the model chain on any answerless reply). The
harness runs non-streaming, so the measured case is covered; a streaming
reasoning-only reply still burns a model.
"""

from __future__ import annotations

from robothor.engine.llm_attempts import (
    OUTCOME_EMPTY,
    OUTCOME_REASONING_ONLY,
    OUTCOME_REASONING_ONLY_RETRY,
    CompletionShape,
    is_attempt_step,
)
from robothor.engine.models import RunStep, StepType


def _measured_shape() -> CompletionShape:
    """The shape from the sweep run, verbatim."""
    return CompletionShape(
        parsed=True,
        finish_reason="length",
        content_present=False,
        tool_calls_present=False,
        reasoning_present=True,
        output_tokens=8192,
        reasoning_tokens=8919,
    )


class TestItIsClassified:
    def test_the_measured_reply_is_reasoning_only_not_empty(self):
        """`empty` routes to the provider-failure path and blames the model.
        This reply is the engine's own budget arithmetic, and the remedy is
        different."""
        shape = _measured_shape()
        assert shape.no_answer
        assert shape.reasoning_only
        assert not shape.empty
        assert shape.outcome == OUTCOME_REASONING_ONLY

    def test_a_true_empty_is_still_empty(self):
        shape = CompletionShape(parsed=True, finish_reason="stop")
        assert shape.outcome == OUTCOME_EMPTY

    def test_the_description_is_the_line_an_operator_greps(self):
        assert _measured_shape().describe() == (
            "finish_reason=length, output_tokens=8192, "
            "reasoning_tokens=8919, reasoning_present=True"
        )


class TestItReachesTheSummary:
    def test_the_attempt_row_is_countable(self):
        """`usage.json` publishes this as `llm_attempts_failed`, separately
        from `request_count`. Folding it into the turn count would make a
        self-healed run look like a run that made twice the calls."""
        step = RunStep(
            run_id="r",
            step_number=1,
            step_type=StepType.LLM_CALL,
            error_message=f"{OUTCOME_REASONING_ONLY}: {_measured_shape().describe()}",
        )
        assert is_attempt_step(step)

    def test_a_self_heal_is_named_apart_from_a_failure(self):
        """A reasoning-only reply the same model recovered from cost latency
        and nothing else. A consumer that cannot tell it from one that gave up
        reports an outage every time the engine fixes itself."""
        healed = RunStep(
            run_id="r",
            step_number=1,
            step_type=StepType.LLM_CALL,
            error_message=f"{OUTCOME_REASONING_ONLY_RETRY}: still thinking",
        )
        assert is_attempt_step(healed)
        assert OUTCOME_REASONING_ONLY_RETRY != OUTCOME_REASONING_ONLY

    def test_a_real_step_is_not_an_attempt_row(self):
        step = RunStep(run_id="r", step_number=1, step_type=StepType.LLM_CALL)
        assert not is_attempt_step(step)


class TestTheReAskExists:
    def test_the_budget_is_one_per_model(self):
        """Bounded for the reason every re-ask here is bounded: an unbounded
        'try again' against a model that keeps overthinking is a loop that
        spends the run's whole wall clock."""
        from robothor.engine.llm_client import REASONING_ONLY_RE_ASKS_PER_MODEL

        assert REASONING_ONLY_RE_ASKS_PER_MODEL == 1

    def test_the_nudge_asks_for_the_answer_not_more_thinking(self):
        from robothor.engine.llm_attempts import REASONING_ONLY_NUDGE

        assert "reply with the answer itself" in REASONING_ONLY_NUDGE
