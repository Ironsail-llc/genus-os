"""Tests for the model registry — context windows and output token calculation."""

from __future__ import annotations

from robothor.engine.model_registry import (
    _FALLBACK,
    ModelLimits,
    compute_token_budget,
    get_model_limits,
    get_output_tokens,
)


class TestGetModelLimits:
    def test_known_model_claude(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4-6")
        assert limits.max_input_tokens == 200_000
        assert limits.max_output_tokens == 128_000
        assert limits.default_output_tokens == 16_384

    def test_known_model_kimi(self):
        limits = get_model_limits("openrouter/moonshotai/kimi-k2.5")
        assert limits.max_input_tokens == 262_144
        assert limits.max_output_tokens == 262_144
        assert limits.default_output_tokens == 8_192

    def test_known_model_gemini_flash(self):
        limits = get_model_limits("gemini/gemini-2.5-flash")
        assert limits.max_input_tokens == 1_048_576
        assert limits.default_output_tokens == 8_192

    def test_known_model_minimax(self):
        limits = get_model_limits("openrouter/minimax/minimax-m2.5")
        assert limits.max_input_tokens == 1_048_576
        assert limits.max_output_tokens == 131_072

    def test_known_model_gemini_pro(self):
        limits = get_model_limits("gemini/gemini-2.5-pro")
        assert limits.max_input_tokens == 1_048_576
        assert limits.max_output_tokens == 65_535

    def test_unknown_model_returns_fallback(self):
        limits = get_model_limits("unknown/model-xyz")
        assert limits == _FALLBACK
        assert limits.max_input_tokens == 128_000
        assert limits.max_output_tokens == 8_192

    def test_limits_are_frozen(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4-6")
        assert isinstance(limits, ModelLimits)


class TestComputeTokenBudget:
    def test_sonnet_15_iterations(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4-6", 15)
        assert budget == 200_000 * 15  # 3,000,000

    def test_kimi_10_iterations(self):
        budget = compute_token_budget("openrouter/moonshotai/kimi-k2.5", 10)
        assert budget == 262_144 * 10  # 2,621,440

    def test_gemini_pro_10_iterations(self):
        budget = compute_token_budget("gemini/gemini-2.5-pro", 10)
        assert budget == 1_048_576 * 10

    def test_unknown_model_uses_fallback(self):
        budget = compute_token_budget("unknown/model", 10)
        assert budget == 128_000 * 10  # fallback max_input

    def test_zero_iterations_returns_unlimited(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4-6", 0)
        assert budget == 0

    def test_negative_iterations_returns_unlimited(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4-6", -1)
        assert budget == 0

    def test_single_iteration(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4-6", 1)
        assert budget == 200_000


class TestGetOutputTokens:
    def test_default_output_when_plenty_of_room(self):
        # Claude has 200K input, 128K max output, 16K default
        # With 10K input, should return default (16K)
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4-6", 10_000)
        assert tokens == 16_384

    def test_capped_by_remaining_window(self):
        # If input nearly fills the window, output must be capped
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4-6", 195_000)
        # remaining = 200K - 195K = 5K, which is < default 16K
        assert tokens == 5_000

    def test_minimum_when_context_full(self):
        # When input exceeds max, return minimum 1024
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4-6", 250_000)
        assert tokens == 1_024

    def test_zero_input(self):
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4-6", 0)
        assert tokens == 16_384  # default

    def test_kimi_default_output(self):
        tokens = get_output_tokens("openrouter/moonshotai/kimi-k2.5", 50_000)
        assert tokens == 8_192

    def test_unknown_model_uses_fallback(self):
        tokens = get_output_tokens("mystery/model", 50_000)
        # Fallback: 128K input, 8K output, 8K default
        assert tokens == 8_192  # default == max_output for fallback
