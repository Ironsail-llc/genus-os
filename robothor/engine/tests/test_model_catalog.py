"""Tests for the catalog-backed model registry (Rip 17 / G6).

Covers: curated registry still wins; unknown-model fallback is the flat default
when off and litellm's bundled catalog when on; and litellm pricing is
single-sourced from _MODEL_REGISTRY (flag on) vs the legacy two-model block
(flag off), ending the historical price drift.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine import model_registry as mr


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    mr._catalog_cache.clear()
    yield
    mr._catalog_cache.clear()


class TestGetModelLimits:
    def test_curated_model_unchanged(self):
        limits = mr.get_model_limits("openrouter/anthropic/claude-sonnet-4.6")
        assert limits.max_input_tokens == 1_000_000
        assert limits.supports_thinking is True

    def test_unknown_model_flat_fallback_when_off(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_RIP_17_ENABLED", raising=False)
        limits = mr.get_model_limits("vendor/never-heard-of-it")
        assert limits is mr._FALLBACK

    def test_unknown_model_uses_litellm_catalog_when_on(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "1")
        info = {
            "max_input_tokens": 400_000,
            "max_output_tokens": 64_000,
            "input_cost_per_token": 0.000_002,
            "output_cost_per_token": 0.000_008,
            "supports_reasoning": True,
        }
        with patch("litellm.get_model_info", return_value=info):
            limits = mr.get_model_limits("vendor/some-real-model")
        assert limits.max_input_tokens == 400_000
        assert limits.max_output_tokens == 64_000
        assert limits.input_cost_per_token == pytest.approx(0.000_002)
        assert limits.supports_thinking is True

    def test_unknown_model_falls_back_when_catalog_empty(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "1")
        with patch("litellm.get_model_info", side_effect=Exception("not found")):
            limits = mr.get_model_limits("vendor/truly-unknown")
        assert limits is mr._FALLBACK

    def test_catalog_result_is_cached(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "1")
        with patch("litellm.get_model_info", side_effect=Exception("x")) as probe:
            mr.get_model_limits("vendor/probe-once")
            mr.get_model_limits("vendor/probe-once")
        # Negative result cached → litellm probed only once.
        assert probe.call_count == 1


class TestRegisterPricing:
    def test_legacy_block_when_off(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_RIP_17_ENABLED", raising=False)
        with patch("litellm.register_model") as reg:
            mr.register_pricing_with_litellm()
        registered = reg.call_args.args[0]
        assert set(registered) == {
            "openrouter/xiaomi/mimo-v2-pro",
            "openrouter/anthropic/claude-sonnet-4.6",
        }

    def test_single_source_when_on(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "1")
        with patch("litellm.register_model") as reg:
            mr.register_pricing_with_litellm()
        registered = reg.call_args.args[0]
        # All curated non-codex models registered from the single source.
        assert "openrouter/deepseek/deepseek-v4-pro" in registered
        assert "openrouter/anthropic/claude-opus-4.7" in registered
        # codex is subscription-billed ($0) → excluded from litellm pricing.
        assert not any(k.startswith("codex/") for k in registered)
        # Prices come straight from the registry (no drift).
        sonnet = registered["openrouter/anthropic/claude-sonnet-4.6"]
        assert sonnet["input_cost_per_token"] == pytest.approx(0.000_003)
