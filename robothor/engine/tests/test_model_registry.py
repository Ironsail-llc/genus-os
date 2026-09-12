"""Tests for the model registry — context windows and output token calculation."""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.model_registry import (
    _FALLBACK,
    _MODEL_REGISTRY,
    THINKING_BUDGET_TOKENS,
    ModelLimits,
    compute_token_budget,
    get_model_limits,
    get_output_tokens,
    register_pricing_with_litellm,
    supports_cache_control,
)


class TestGetModelLimits:
    def test_known_model_claude(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4.6")
        assert limits.max_input_tokens == 1_000_000
        assert limits.max_output_tokens == 128_000
        assert limits.default_output_tokens == 16_384

    def test_known_model_mimo_v2_pro(self):
        limits = get_model_limits("openrouter/xiaomi/mimo-v2-pro")
        assert limits.max_input_tokens == 1_000_000
        assert limits.max_output_tokens == 65_536
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

    def test_qwen35_removed(self):
        """Qwen 3.5 was removed — should return fallback now."""
        limits = get_model_limits("ollama_chat/qwen3.5:122b")
        assert limits == _FALLBACK

    def test_gpt_5_5_pro_removed(self):
        """GPT-5.5 Pro was retired 2026-04-25 (cost) — absent from the static registry.

        (get_model_limits may now size it via litellm's dynamic catalog; the
        contract these tests guard is that it's not in the hand-maintained dict.)
        """
        from robothor.engine.model_registry import _MODEL_REGISTRY

        assert "openrouter/openai/gpt-5.5-pro" not in _MODEL_REGISTRY

    def test_opus_4_6_removed(self):
        """Opus 4.6 was superseded by 4.7 — absent from the static registry."""
        from robothor.engine.model_registry import _MODEL_REGISTRY

        assert "openrouter/anthropic/claude-opus-4.6" not in _MODEL_REGISTRY

    def test_unknown_model_returns_fallback(self):
        limits = get_model_limits("unknown/model-xyz")
        assert limits == _FALLBACK
        assert limits.max_input_tokens == 128_000
        assert limits.max_output_tokens == 8_192

    def test_dated_response_slug_resolves_to_registry_entry(self):
        """OpenRouter responses carry a dated slug (e.g. xiaomi/mimo-v2.5-20260422)
        without the openrouter/ prefix. Cost fallback and context sizing look
        these up — they must resolve to the curated entry, not the 128K/zero-cost
        fallback."""
        base = get_model_limits("openrouter/xiaomi/mimo-v2.5")
        assert base is not _FALLBACK
        assert get_model_limits("xiaomi/mimo-v2.5-20260422") == base
        assert get_model_limits("openrouter/xiaomi/mimo-v2.5-20260422") == base
        pro = get_model_limits("openrouter/xiaomi/mimo-v2.5-pro")
        assert get_model_limits("xiaomi/mimo-v2.5-pro-20260422") == pro

    def test_unprefixed_response_slug_resolves(self):
        """A bare provider/model slug from a response resolves to its
        openrouter/-prefixed registry entry."""
        assert get_model_limits("xiaomi/mimo-v2.5") == get_model_limits(
            "openrouter/xiaomi/mimo-v2.5"
        )

    def test_limits_are_frozen(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4.6")
        assert isinstance(limits, ModelLimits)

    def test_claude_supports_thinking(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4.6")
        assert limits.supports_thinking is True

    def test_mimo_v2_pro_no_thinking(self):
        limits = get_model_limits("openrouter/xiaomi/mimo-v2-pro")
        assert limits.supports_thinking is False

    def test_thinking_budget_constant(self):
        assert THINKING_BUDGET_TOKENS == 10_000

    def test_no_default_thinking_budget_field(self):
        """default_thinking_budget field was removed from ModelLimits."""
        assert not hasattr(ModelLimits, "default_thinking_budget")


class TestComputeTokenBudget:
    def test_sonnet_15_iterations(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4.6", 15)
        assert budget == 1_000_000 * 15  # 15,000,000

    def test_mimo_v2_pro_10_iterations(self):
        budget = compute_token_budget("openrouter/xiaomi/mimo-v2-pro", 10)
        assert budget == 1_000_000 * 10  # 10,000,000

    def test_gemini_pro_10_iterations(self):
        budget = compute_token_budget("gemini/gemini-2.5-pro", 10)
        assert budget == 1_048_576 * 10

    def test_unknown_model_uses_fallback(self):
        budget = compute_token_budget("unknown/model", 10)
        assert budget == 128_000 * 10  # fallback max_input

    def test_zero_iterations_returns_unlimited(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4.6", 0)
        assert budget == 0

    def test_negative_iterations_returns_unlimited(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4.6", -1)
        assert budget == 0

    def test_single_iteration(self):
        budget = compute_token_budget("openrouter/anthropic/claude-sonnet-4.6", 1)
        assert budget == 1_000_000


class TestSupportsCacheControl:
    """Catalog-driven cache_control capability (PR 4).

    Pins the *current* fleet behavior of the ``cache_control`` gate that used
    to be a bare ``model.startswith("anthropic/")`` check in
    ``LLMClient._build_llm_kwargs``: direct Anthropic models still get it,
    OpenRouter models (including openrouter/anthropic/*) still don't, and
    codex/* custom-provider models still don't (litellm has no catalog entry
    for them, so the capability lookup falls through to False).
    """

    def test_direct_anthropic_is_true(self):
        assert supports_cache_control("anthropic/claude-sonnet-4-6") is True

    def test_openrouter_anthropic_is_false(self):
        """The mixed content-block format breaks tool_use/tool_result pairing
        on OpenRouter's OpenAI-compatible path — excluded regardless of what
        litellm's catalog says about the underlying Claude model."""
        assert supports_cache_control("openrouter/anthropic/claude-sonnet-4.6") is False

    def test_openrouter_non_anthropic_is_false(self):
        assert supports_cache_control("openrouter/xiaomi/mimo-v2.5-pro") is False

    def test_codex_is_false(self):
        """codex/* is a custom subscription provider litellm doesn't catalog."""
        assert supports_cache_control("codex/gpt-5.5") is False

    def test_unknown_model_defaults_false(self):
        assert supports_cache_control("unknown/model-xyz") is False

    def test_uncataloged_anthropic_model_is_false(self):
        """Regression (review finding): NO anthropic/-prefix fallback.

        litellm's ``supports_prompt_caching`` returns False both for models
        explicitly marked unsupported and for ids it simply hasn't mapped —
        the two are indistinguishable from the return value. A prefix
        fallback treating False as "catalog lag, assume True" would also
        return True for a genuinely caching-unsupported anthropic model,
        making this lookup an untrustworthy capability oracle. The chain is
        strictly: curated override → litellm → default False. Catalog lag
        for a newly-released fleet model is handled by adding a curated
        ``supports_cache_control=True`` override to ``_MODEL_REGISTRY``
        (next test)."""
        assert supports_cache_control("anthropic/claude-uncataloged-test-sentinel") is False

    def test_curated_override_covers_catalog_lag_for_new_anthropic_model(self):
        """The supported path for a new direct-Anthropic model litellm hasn't
        cataloged yet: pin it in the curated registry."""
        fake_limits = ModelLimits(
            max_input_tokens=200_000,
            max_output_tokens=64_000,
            default_output_tokens=16_384,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
            supports_cache_control=True,
        )
        with patch.dict(
            "robothor.engine.model_registry._MODEL_REGISTRY",
            {"anthropic/claude-mythos-preview": fake_limits},
        ):
            assert supports_cache_control("anthropic/claude-mythos-preview") is True

    def test_curated_override_beats_litellm(self):
        """An explicit ``ModelLimits.supports_cache_control`` wins over both
        the OpenRouter blanket-exclusion and the litellm fallback."""
        fake_limits = ModelLimits(
            max_input_tokens=100_000,
            max_output_tokens=8_192,
            default_output_tokens=4_096,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
            supports_cache_control=True,
        )
        with patch.dict(
            "robothor.engine.model_registry._MODEL_REGISTRY",
            {"openrouter/future/pairing-fixed": fake_limits},
        ):
            assert supports_cache_control("openrouter/future/pairing-fixed") is True

    def test_curated_override_can_force_false(self):
        fake_limits = ModelLimits(
            max_input_tokens=100_000,
            max_output_tokens=8_192,
            default_output_tokens=4_096,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
            supports_cache_control=False,
        )
        with patch.dict(
            "robothor.engine.model_registry._MODEL_REGISTRY",
            {"anthropic/some-future-model": fake_limits},
        ):
            assert supports_cache_control("anthropic/some-future-model") is False


class TestGetOutputTokens:
    def test_default_output_when_plenty_of_room(self):
        # Claude has 1M input, 128K max output, 16K default
        # With 10K input, should return default (16K)
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4.6", 10_000)
        assert tokens == 16_384

    def test_capped_by_remaining_window(self):
        # If input nearly fills the window, output must be capped
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4.6", 995_000)
        # remaining = 1M - 995K = 5K, which is < default 16K
        assert tokens == 5_000

    def test_minimum_when_context_full(self):
        # When input exceeds max, return minimum 1024
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4.6", 1_100_000)
        assert tokens == 1_024

    def test_zero_input(self):
        tokens = get_output_tokens("openrouter/anthropic/claude-sonnet-4.6", 0)
        assert tokens == 16_384  # default

    def test_mimo_v2_pro_default_output(self):
        tokens = get_output_tokens("openrouter/xiaomi/mimo-v2-pro", 50_000)
        assert tokens == 8_192

    def test_unknown_model_uses_fallback(self):
        tokens = get_output_tokens("mystery/model", 50_000)
        # Fallback: 128K input, 8K output, 8K default
        assert tokens == 8_192  # default == max_output for fallback


def _merging_register(captured: dict):
    """A ``litellm.register_model`` stand-in with litellm's merge semantics.

    litellm merges a partial payload into an existing entry rather than
    replacing it — which is the whole reason a capability-only registration is
    safe beside a pricing one. A fake that clobbered instead would fail this
    module's tests for a reason litellm does not have.
    """

    def register(payload: dict) -> None:
        for model_id, fields in payload.items():
            captured.setdefault(model_id, {}).update(fields)

    return register


class TestLitellmReasoningRegistration:
    """The registry must teach litellm which of our models reason.

    ``llm_client._build_kwargs`` sends an Anthropic-style ``thinking`` block
    whenever ``ModelLimits.supports_thinking`` is set. litellm accepts that
    parameter only for a model its own *bundled* cost map marks
    ``supports_reasoning`` — and a model registered the week it launches is
    never in that bundled map. The result is not a degraded call, it is
    ``UnsupportedParamsError`` raised in-process before the request leaves:
    on 2026-09-11 a freshly registered ``openrouter/deepseek/deepseek-v4.1-flash``
    failed 100% of its benchmark tasks this way while the older ids beside it
    passed.

    The capability pass is deliberately NOT behind ``ROBOTHOR_RIP_17_ENABLED``.
    That flag chooses where *pricing* comes from; the reasoning capability is a
    correctness precondition for the call happening at all, and rip 17 is off
    on this instance — a fix that only ran with the flag on would have been
    inert exactly where the outage was.
    """

    @staticmethod
    def _thinking_models() -> list[str]:
        return [
            m
            for m, lim in _MODEL_REGISTRY.items()
            if lim.supports_thinking and not m.startswith("codex/")
        ]

    def test_capability_published_with_catalog_mode_off(self):
        """Rip 17 off — the legacy pricing branch — still publishes reasoning."""
        captured: dict[str, dict] = {}

        with (
            patch("litellm.register_model", _merging_register(captured)),
            patch(
                "robothor.engine.feature_flags.catalog_backed_models_enabled",
                return_value=False,
            ),
        ):
            register_pricing_with_litellm()

        for model_id in self._thinking_models():
            assert captured.get(model_id, {}).get("supports_reasoning") is True, model_id

    def test_capability_published_with_catalog_mode_on(self):
        """Rip 17 on — the curated pricing branch — publishes it too."""
        captured: dict[str, dict] = {}

        with (
            patch("litellm.register_model", _merging_register(captured)),
            patch(
                "robothor.engine.feature_flags.catalog_backed_models_enabled",
                return_value=True,
            ),
        ):
            register_pricing_with_litellm()

        for model_id in self._thinking_models():
            assert captured.get(model_id, {}).get("supports_reasoning") is True, model_id

    def test_capability_only_widens(self):
        """A curated ``supports_thinking=False`` never publishes a False.

        litellm's own map is the better authority for a model we have not
        marked as reasoning, and the engine does not send ``thinking`` for one
        anyway. Narrowing would take a working parameter away from a model on
        no evidence.
        """
        captured: dict[str, dict] = {}

        with patch("litellm.register_model", _merging_register(captured)):
            register_pricing_with_litellm()

        for model_id, limits in _MODEL_REGISTRY.items():
            if not limits.supports_thinking:
                assert captured.get(model_id, {}).get("supports_reasoning") is not False, model_id

    def test_thinking_model_is_accepted_by_litellm_after_registration(self):
        """The end-to-end symptom: litellm takes ``thinking`` for the new id.

        Asserted against litellm's real parameter table rather than our own
        payload, because a correct payload is not what was missing — the engine
        never told litellm at all.
        """
        import litellm

        model_id = "openrouter/deepseek/deepseek-v4.1-flash"
        assert get_model_limits(model_id).supports_thinking is True

        register_pricing_with_litellm()

        assert "thinking" in (litellm.get_supported_openai_params(model=model_id) or [])
