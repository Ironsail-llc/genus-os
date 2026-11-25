"""Real model calls for the answer arm. Run only under the ``llm`` marker."""

from __future__ import annotations

import pytest

from robothor.memory import eval_answer as ea
from robothor.memory import eval_world as ew
from robothor.memory.eval_systems import ContextBundle, fill_budget

pytestmark = pytest.mark.llm


@pytest.mark.asyncio
async def test_real_model_answers_from_context_and_abstains_without_it():
    bundle = ContextBundle(
        system="t",
        items=fill_budget(
            [("fct-1", "fact", "In March 2021, Alice Ashby joined Kelvaro Systems as designer.")],
            500,
        ),
        budget=500,
        equal_budget=True,
        abstained=False,
        retrieval_ms=0.0,
    )
    got = await ea.answer("Which organisation does Alice Ashby work for?", bundle)
    assert "kelvaro" in got.text.lower() and not got.abstained
    empty = ContextBundle("t", (), 500, True, False, 0.0)
    none = await ea.answer("What is Alice Ashby's blood type?", empty)
    assert none.abstained


@pytest.mark.asyncio
async def test_real_llm_judge_passes_validation_on_the_full_world():
    """On the size baselines are recorded on. The small world has only eight
    organisations, so one strict call on one organisation's bare name (the
    judge rejected "Orvvaro" for "Orvvaro Logistics" seven times, once per
    case) moves a whole planted kind below the per-kind gate."""
    world = ew.generate_world(1, "full")
    result = await ea.validate_judge(world, ea.make_judge(world, llm=ea.llm_judge))
    assert result.passed, result
