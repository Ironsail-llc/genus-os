"""The judge must be given room to answer, or it never answers at all.

Measured 2026-08-22 against a real four-item rubric and a realistic agent
output, on the configured judge model (a reasoning model):

    max_tokens=200  -> 3/3 empty, finish_reason=length every time
    max_tokens=1200 -> 0/3 empty

The cap was 200. A reasoning model spends its budget thinking before it emits
any content, so the call returned a 200 OK carrying nothing. That is
deterministic, not transient — which is exactly why JUDGE_ATTEMPTS=3 never
rescued it: all three attempts hit the same wall.

The cost was fleet-wide and invisible. In the 2026-08-22 fleet pass, **12 of 40
counted failures (30%) were "judge returned an empty completion"**, across 9 of
19 agents, one of them scored a hard 0.0. Every one of those was filed against
the agent.

This is the same shape as the rest of the campaign: an infrastructure limit,
recorded as the agent's fault.
"""

from __future__ import annotations

import inspect

from robothor.engine.tools.handlers import benchmark

#: Below this, a reasoning judge returns nothing at all. Measured, not guessed:
#: 200 was empty 3/3 and 1200 was empty 0/3 on the live model. The floor sits
#: above 1200 because one of those 1200-token attempts still came back
#: finish_reason=length — the margin is thin, and a starved judge is scored as a
#: mediocre agent rather than as a broken instrument.
MIN_JUDGE_MAX_TOKENS = 1500


def test_judge_token_budget_is_not_starved() -> None:
    assert benchmark.JUDGE_MAX_TOKENS >= MIN_JUDGE_MAX_TOKENS, (
        f"JUDGE_MAX_TOKENS={benchmark.JUDGE_MAX_TOKENS} starves a reasoning judge: "
        "measured empty 3/3 at 200 tokens. A judge that cannot answer is not a "
        "mediocre agent."
    )


def test_the_judge_call_uses_the_constant_not_a_literal() -> None:
    """A literal here is how the cap drifted out of anyone's view for months."""
    src = inspect.getsource(benchmark._judge_output)
    assert "JUDGE_MAX_TOKENS" in src, "the judge call must reference the constant"
    assert "max_tokens=200" not in src, "the starving literal is back"


def test_retry_count_is_still_honoured() -> None:
    """Raising the budget must not quietly remove the transient-retry path."""
    assert benchmark.JUDGE_ATTEMPTS >= 2
