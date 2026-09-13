"""The thinking block is sized against the answer it has to leave room for.

DIAG 2026-09-13 §4.2, `llm_client.py:1352-1358`. Three defects in six lines:

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
"""

from __future__ import annotations

import pytest

from robothor.engine.llm_client import MIN_ANSWER_TOKENS, LLMClient, _thinking_kwargs
from robothor.engine.model_registry import (
    _REASONING_BUDGETS,
    get_model_limits,
    set_reasoning_effort,
)

THINKING_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
ANTHROPIC_THINKING_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
MESSAGES = [{"role": "user", "content": "hi"}]


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


def test_thinking_budget_honours_agent_reasoning_effort() -> None:
    """Fails today: the builder imports the constant, not the ContextVar."""
    set_reasoning_effort("low")
    low = _kwargs(THINKING_MODEL)["thinking"]["budget_tokens"]
    set_reasoning_effort("high")
    high = _kwargs(THINKING_MODEL)["thinking"]["budget_tokens"]
    assert low == _REASONING_BUDGETS["low"]
    assert low < high


@pytest.mark.parametrize("model", _thinking_models())
@pytest.mark.parametrize("effort", sorted(_REASONING_BUDGETS))
def test_thinking_budget_never_exceeds_max_tokens(model: str, effort: str) -> None:
    set_reasoning_effort(effort)
    kwargs = _kwargs(model)
    thinking = kwargs.get("thinking")
    if thinking is None:
        # A model whose answer budget cannot host a usable thinking block gets
        # no thinking block at all — that is the clamp doing its job.
        return
    budget = thinking["budget_tokens"]
    assert budget < kwargs["max_tokens"]
    assert budget <= kwargs["max_tokens"] // 2, (
        "the clamp must leave room for the answer, not merely fit inside the cap"
    )


def test_temperature_is_not_forced_for_a_non_anthropic_thinking_model() -> None:
    assert _kwargs(THINKING_MODEL, temperature=0.3)["temperature"] == 0.3


def test_temperature_is_still_forced_for_anthropic() -> None:
    """Anthropic's API rejects thinking with any temperature but 1.0."""
    assert _kwargs(ANTHROPIC_THINKING_MODEL, temperature=0.3)["temperature"] == 1.0


def test_a_non_thinking_model_gets_no_thinking_block() -> None:
    assert "thinking" not in _kwargs("openrouter/openai/gpt-4o-mini")


def test_the_clamp_binds_on_the_real_deepseek_numbers() -> None:
    """16,384 default output; `max` effort asks for 48,000 (DIAG §4.2)."""
    limits = get_model_limits(THINKING_MODEL)
    assert limits.default_output_tokens == 16_384
    set_reasoning_effort("max")
    kwargs = _kwargs(THINKING_MODEL)
    budget = kwargs["thinking"]["budget_tokens"]
    assert budget <= 16_384 - MIN_ANSWER_TOKENS
    assert kwargs["max_tokens"] - budget >= MIN_ANSWER_TOKENS


def test_an_answer_budget_too_small_for_thinking_gets_none() -> None:
    """A near-full context window leaves no room for a valid block; send neither."""
    assert _thinking_kwargs(THINKING_MODEL, 1_000) == {}
    assert "thinking" in _thinking_kwargs(THINKING_MODEL, 8_192)
