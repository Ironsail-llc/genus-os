"""The thinking block is sized against the answer it has to leave room for.

DIAG 2026-09-13 §4.2 and the hostile review's I3/M1/M2.

1. `budget_tokens=10_000` against a `max_tokens=16_384` request is 61% of the
   completion spent on reasoning, with no guard that the two are related at
   all. `max` effort (48,000) against the same 16,384 default was a standing
   landmine.
2. `current_thinking_budget()` (`model_registry.py:878-880`) had exactly one
   caller in the tree — a unit test. `runner.py:628-630` sets the per-agent
   `reasoning_effort` on every run, and the kwargs builder imported the bare
   CONSTANT, so every agent on the box ran at `medium` whatever its manifest
   said. Another armed-but-aimed-at-nothing control.
3. `temperature = 1.0  # Required by Anthropic API` was applied to every
   `supports_thinking` model. The comment stopped matching its code when the
   flag was extended past Anthropic, and the fleet's CRM dedup and email
   classification have been running at maximum-entropy sampling since.

The first fix clamped an ABSOLUTE budget, and the review found that half-done:
every `supports_thinking` model in the fleet requests the same 16,384 output,
so medium/high/max all clamped to one number and three of the four manifest
rungs were still indistinguishable — and the clamp was non-monotone
(`max_tokens=5120` got a SMALLER budget than 4096). The budget is now a SHARE
of the completion, which is monotone in both arguments by construction.
"""

from __future__ import annotations

import pytest

from robothor.engine.llm_client import (
    _EFFORT_THINKING_SHARE,
    MIN_ANSWER_TOKENS,
    MIN_THINKING_BUDGET,
    LLMClient,
    _thinking_budget,
    _thinking_kwargs,
)
from robothor.engine.model_registry import (
    _REASONING_BUDGETS,
    get_model_limits,
    set_reasoning_effort,
)

THINKING_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
ANTHROPIC_THINKING_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
MESSAGES = [{"role": "user", "content": "hi"}]
RUNGS = ("low", "medium", "high", "max")


@pytest.fixture(autouse=True)
def _default_effort():
    """The ContextVar outlives a test otherwise."""
    set_reasoning_effort("medium")
    yield
    set_reasoning_effort("medium")


def _kwargs(model: str, *, temperature: float = 0.3) -> dict:
    return LLMClient._build_llm_kwargs(model, MESSAGES, [], 100, temperature)


def _thinking_models() -> list[str]:
    from robothor.engine.model_registry import _MODEL_REGISTRY

    return [m for m, limits in _MODEL_REGISTRY.items() if limits.supports_thinking]


def test_there_are_thinking_models_to_test() -> None:
    """A parametrisation over an empty list passes vacuously."""
    assert _thinking_models()


def test_every_manifest_rung_has_a_share() -> None:
    """A rung with no share silently falls back to medium."""
    assert set(_REASONING_BUDGETS) == set(_EFFORT_THINKING_SHARE)


# ─── the knob actually moves ────────────────────────────────────────────


@pytest.mark.parametrize("model", _thinking_models())
def test_the_rungs_are_strictly_monotone_for_every_fleet_model(model: str) -> None:
    """I3: medium/high/max used to be the same number on every fleet model."""
    budgets = []
    for effort in ("low", "medium", "high"):
        set_reasoning_effort(effort)
        budgets.append(_kwargs(model)["thinking"]["budget_tokens"])
    assert budgets[0] < budgets[1] < budgets[2], (
        f"{model}: reasoning_effort must change what reaches the wire, got {budgets}"
    )
    set_reasoning_effort("max")
    assert _kwargs(model)["thinking"]["budget_tokens"] >= budgets[2]


@pytest.mark.parametrize("model", _thinking_models())
@pytest.mark.parametrize("effort", RUNGS)
def test_the_answer_always_keeps_its_room(model: str, effort: str) -> None:
    set_reasoning_effort(effort)
    kwargs = _kwargs(model)
    thinking = kwargs.get("thinking")
    if thinking is None:
        # A model whose answer budget cannot host a usable thinking block gets
        # no thinking block at all — that is the clamp doing its job.
        return
    budget = thinking["budget_tokens"]
    assert budget < kwargs["max_tokens"]
    assert kwargs["max_tokens"] - budget >= MIN_ANSWER_TOKENS


def test_min_answer_tokens_is_load_bearing() -> None:
    """M1: the constant carried the justification and bound nothing.

    Mutating it to 0 must break this — at the top rung it is the only thing
    standing between the share and the whole completion.
    """
    set_reasoning_effort("max")
    assert _thinking_budget(16_384, "max") == 16_384 - MIN_ANSWER_TOKENS


@pytest.mark.parametrize("effort", RUNGS)
def test_the_budget_never_shrinks_as_the_ceiling_grows(effort: str) -> None:
    """M2: `max_tokens=4096` once bought MORE thinking than 5120 did.

    Reachable in production — `get_output_tokens` returns the window remainder
    when the context is nearly full, which lands squarely in that band.
    """
    budgets = [_thinking_budget(mt, effort) for mt in range(512, 32_768, 256)]
    assert budgets == sorted(budgets)
    assert all(b >= 0 for b in budgets)


def test_the_re_ask_asks_for_strictly_less_at_every_rung() -> None:
    for effort in RUNGS:
        full = _thinking_budget(16_384, effort)
        reduced = _thinking_budget(16_384, effort, reduced=True)
        assert reduced < full, effort


# ─── temperature ────────────────────────────────────────────────────────


def test_temperature_is_not_forced_for_a_non_anthropic_thinking_model() -> None:
    assert _kwargs(THINKING_MODEL, temperature=0.3)["temperature"] == 0.3


def test_temperature_is_still_forced_for_anthropic() -> None:
    """Anthropic's API rejects thinking with any temperature but 1.0."""
    assert _kwargs(ANTHROPIC_THINKING_MODEL, temperature=0.3)["temperature"] == 1.0


def test_a_non_thinking_model_gets_no_thinking_block() -> None:
    assert "thinking" not in _kwargs("openrouter/openai/gpt-4o-mini")


# ─── the clamp on the numbers the fleet actually sends ──────────────────


def test_the_clamp_binds_on_the_real_deepseek_numbers() -> None:
    """16,384 default output; `max` effort asked for 48,000 (DIAG §4.2)."""
    limits = get_model_limits(THINKING_MODEL)
    assert limits.default_output_tokens == 16_384
    set_reasoning_effort("max")
    kwargs = _kwargs(THINKING_MODEL)
    budget = kwargs["thinking"]["budget_tokens"]
    assert budget == 16_384 - MIN_ANSWER_TOKENS
    assert budget < 48_000, "the rung asks for 3x the whole completion"


def test_an_answer_budget_too_small_for_thinking_gets_none() -> None:
    """A near-full context window leaves no room for a valid block."""
    assert _thinking_kwargs(THINKING_MODEL, 1_000) == {}
    assert _thinking_kwargs(THINKING_MODEL, 2 * MIN_THINKING_BUDGET)["thinking"]
