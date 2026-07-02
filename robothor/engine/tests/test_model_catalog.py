"""Dynamic model catalog + reasoning-effort config (Wave-2, W2-12).

get_model_limits now consults litellm for models not in the static registry
(instead of silently using a 128K fallback) and warns when even that misses.
Reasoning effort maps to a per-run thinking-token budget via a ContextVar.
"""

from __future__ import annotations

import logging

import robothor.engine.model_registry as mr


class TestDynamicCatalog:
    def test_static_registry_wins(self):
        known = next(iter(mr._MODEL_REGISTRY))
        assert mr.get_model_limits(known) is mr._MODEL_REGISTRY[known]

    def test_litellm_fallback_used_for_unknown(self, monkeypatch):
        mr._dynamic_model_limits.cache_clear()
        fake = mr.ModelLimits(
            max_input_tokens=200_000,
            max_output_tokens=16_384,
            default_output_tokens=8_192,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
        )
        monkeypatch.setattr(mr, "_dynamic_model_limits", lambda m: fake)
        assert mr.get_model_limits("some/unknown-model").max_input_tokens == 200_000

    def test_unknown_everywhere_warns_and_falls_back(self, monkeypatch, caplog):
        mr._dynamic_model_limits.cache_clear()
        monkeypatch.setattr(mr, "_dynamic_model_limits", lambda m: None)
        with caplog.at_level(logging.WARNING, logger="robothor.engine.model_registry"):
            limits = mr.get_model_limits("totally/made-up-xyz")
        assert limits is mr._FALLBACK
        assert any("Unknown model" in r.getMessage() for r in caplog.records)


class TestReasoningEffort:
    def test_budget_levels(self):
        assert mr.reasoning_budget_tokens("low") == 2_000
        assert mr.reasoning_budget_tokens("medium") == mr.THINKING_BUDGET_TOKENS
        assert mr.reasoning_budget_tokens("high") == 24_000
        assert mr.reasoning_budget_tokens("max") == 48_000

    def test_unknown_effort_defaults_medium(self):
        assert mr.reasoning_budget_tokens("bogus") == mr.THINKING_BUDGET_TOKENS
        assert mr.reasoning_budget_tokens("") == mr.THINKING_BUDGET_TOKENS

    def test_contextvar_roundtrip(self):
        mr.set_reasoning_effort("high")
        assert mr.current_thinking_budget() == 24_000
        mr.set_reasoning_effort("medium")
        assert mr.current_thinking_budget() == mr.THINKING_BUDGET_TOKENS
