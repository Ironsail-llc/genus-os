"""A model registry entry knows whether it is still a model we recommend.

The operator's complaint on 2026-09-11 was "the default models are outdated,
the picker models are outdated". The registry had no way to say so: every
entry looked equally current, including two whose OpenRouter ids no longer
resolve at all (``openrouter/stealth/ox-alpha`` de-cloaked and vanished,
``openrouter/xiaomi/mimo-v2-pro`` was withdrawn — both verified against the
live catalog on 2026-09-11).

``ModelLimits.status`` carries that fact, and the rules it buys are:

* ``deprecated`` entries stay RESOLVABLE — a manifest that names one must keep
  validating, because deleting the entry would fall through to the 128K
  conservative fallback and silently mis-size and mis-price every run. It
  warns instead, and the warning names the replacement.
* nothing that LISTS models for a human to choose from may offer a
  ``deprecated`` one. On this instance that is exactly one surface: the
  Telegram ``/model`` keyboard.
* ``legacy`` is still offered, still correct, just no longer the pick.
"""

from __future__ import annotations

from robothor.engine.model_registry import (
    _MODEL_REGISTRY,
    ModelLimits,
    get_model_limits,
    list_models,
)

#: The ids Part A was asked to mark dead. Two of them (ox-alpha, mimo-v2-pro)
#: also fail the liveness check against OpenRouter's catalog.
DEPRECATED_IDS = [
    "openrouter/z-ai/glm-5",
    "openrouter/xiaomi/mimo-v2-pro",
    "openrouter/minimax/minimax-m2.5",
    "openrouter/stealth/ox-alpha",
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-pro",
    "codex/gpt-5.3-codex",
]

NEW_IDS = [
    "openrouter/deepseek/deepseek-v4.1-flash",
    "openrouter/z-ai/glm-5.3-flash",
]


class TestStatusField:
    def test_status_defaults_to_current(self):
        """An entry that says nothing is a model we still recommend."""
        limits = ModelLimits(
            max_input_tokens=1000,
            max_output_tokens=100,
            default_output_tokens=100,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
        )
        assert limits.status == "current"
        assert limits.replaced_by == ""

    def test_every_registry_status_is_a_known_value(self):
        allowed = {"current", "legacy", "deprecated"}
        bad = {
            mid: limits.status
            for mid, limits in _MODEL_REGISTRY.items()
            if limits.status not in allowed
        }
        assert not bad, f"unknown status values: {bad}"

    def test_deprecated_entries_are_marked(self):
        for model_id in DEPRECATED_IDS:
            assert model_id in _MODEL_REGISTRY, f"{model_id} vanished from the registry"
            assert _MODEL_REGISTRY[model_id].status == "deprecated", model_id

    def test_deprecated_entries_name_a_replacement(self):
        """A warning that only says 'deprecated' tells an operator nothing."""
        for model_id in DEPRECATED_IDS:
            replacement = _MODEL_REGISTRY[model_id].replaced_by
            assert replacement, f"{model_id} is deprecated with no replacement named"
            assert replacement in _MODEL_REGISTRY, (
                f"{model_id} points at {replacement!r}, which is not a registry entry"
            )
            assert _MODEL_REGISTRY[replacement].status != "deprecated", (
                f"{model_id} is replaced by another deprecated model"
            )

    def test_deprecated_entries_still_resolve(self):
        """Deleting them would silently fall through to the 128K fallback."""
        from robothor.engine.model_registry import _FALLBACK

        for model_id in DEPRECATED_IDS:
            limits = get_model_limits(model_id)
            assert limits is not _FALLBACK, f"{model_id} fell through to the fallback"
            assert limits.max_input_tokens > 0

    def test_deepseek_v4_flash_is_legacy_not_deprecated(self):
        """Still a valid fallback, just no longer the recommendation."""
        assert _MODEL_REGISTRY["openrouter/deepseek/deepseek-v4-flash"].status == "legacy"


class TestNewCandidates:
    def test_new_models_resolve(self):
        from robothor.engine.model_registry import _FALLBACK

        for model_id in NEW_IDS:
            limits = get_model_limits(model_id)
            assert limits is not _FALLBACK, f"{model_id} is not registered"

    def test_deepseek_v41_flash_limits_match_openrouter(self):
        """Read from OpenRouter's live model metadata on 2026-09-11."""
        limits = get_model_limits("openrouter/deepseek/deepseek-v4.1-flash")
        assert limits.max_input_tokens == 1_048_576
        assert limits.max_output_tokens == 384_000
        assert limits.input_cost_per_token == 0.000_000_15  # $0.150/M
        assert limits.output_cost_per_token == 0.000_000_6  # $0.600/M
        assert limits.supports_thinking is True
        assert limits.status == "current"

    def test_glm_53_flash_limits_match_openrouter(self):
        limits = get_model_limits("openrouter/z-ai/glm-5.3-flash")
        assert limits.max_input_tokens == 1_048_576
        assert limits.max_output_tokens == 131_072
        assert limits.input_cost_per_token == 0.000_000_15  # $0.150/M
        assert limits.output_cost_per_token == 0.000_000_5  # $0.500/M
        assert limits.supports_thinking is True
        assert limits.status == "current"

    def test_new_models_resolve_from_the_provider_echo_form(self):
        """OpenRouter echoes back a dated slug with no routing prefix."""
        limits = get_model_limits("deepseek/deepseek-v4.1-flash-20260901")
        assert limits.max_output_tokens == 384_000

    def test_mimo_v25_input_price_matches_the_live_catalog(self):
        """The control model's price was 25% under the billed rate.

        Registered $0.105/M against OpenRouter's $0.140/M, so every cost
        figure this instance computed for its own fleet primary — including
        the benchmark's cost-per-success — was understated.
        """
        limits = get_model_limits("openrouter/xiaomi/mimo-v2.5")
        assert limits.input_cost_per_token == 0.000_000_14


class TestListModels:
    def test_listing_hides_deprecated(self):
        listed = {m.model_id for m in list_models()}
        for model_id in DEPRECATED_IDS:
            assert model_id not in listed, f"{model_id} is still offered to a human"

    def test_listing_orders_current_before_legacy(self):
        statuses = [m.status for m in list_models()]
        assert statuses, "list_models returned nothing"
        assert statuses == sorted(statuses, key=lambda s: 0 if s == "current" else 1), (
            f"legacy entries are interleaved with current ones: {statuses}"
        )

    def test_listing_includes_the_new_candidates(self):
        listed = {m.model_id for m in list_models()}
        for model_id in NEW_IDS:
            assert model_id in listed

    def test_listing_carries_the_status_label(self):
        legacy = [m for m in list_models() if m.status == "legacy"]
        assert legacy, "nothing is marked legacy — the label would be untestable"
        assert all(m.label for m in legacy)

    def test_include_deprecated_is_opt_in(self):
        listed = {m.model_id for m in list_models(include_deprecated=True)}
        assert "openrouter/stealth/ox-alpha" in listed


class TestManifestValidationWarnsOnDeprecated:
    def test_deprecated_primary_validates_with_a_warning_naming_the_replacement(self):
        from robothor.engine.config_schema import validate_manifest

        manifest = {
            "agent_id": "probe",
            "model": {"primary": "openrouter/xiaomi/mimo-v2-pro"},
        }
        warnings = validate_manifest(manifest)
        joined = " ".join(warnings)
        assert "mimo-v2-pro" in joined
        assert "deprecated" in joined.lower()
        replacement = _MODEL_REGISTRY["openrouter/xiaomi/mimo-v2-pro"].replaced_by
        assert replacement in joined, f"warning does not name {replacement}: {joined}"

    def test_a_current_primary_produces_no_model_warning(self):
        from robothor.engine.config_schema import validate_manifest

        manifest = {
            "agent_id": "probe",
            "model": {"primary": "openrouter/deepseek/deepseek-v4.1-flash"},
        }
        warnings = validate_manifest(manifest)
        assert not [w for w in warnings if "deprecated" in w.lower()]


class TestTelegramPickerExcludesDeprecated:
    """The one surface on this instance that lists models for a human."""

    def test_no_picker_entry_is_deprecated(self):
        from robothor.engine.telegram_handlers import AVAILABLE_MODELS

        for display, model_id in AVAILABLE_MODELS.items():
            limits = _MODEL_REGISTRY.get(model_id)
            assert limits is not None, f"picker entry {display!r} is not registered"
            assert limits.status != "deprecated", f"picker offers deprecated {model_id}"

    def test_picker_offers_the_new_candidates(self):
        from robothor.engine.telegram_handlers import AVAILABLE_MODELS

        for model_id in NEW_IDS:
            assert model_id in AVAILABLE_MODELS.values(), f"{model_id} is not offered"

    def test_picker_lists_current_before_legacy(self):
        from robothor.engine.telegram_handlers import AVAILABLE_MODELS

        statuses = [_MODEL_REGISTRY[m].status for m in AVAILABLE_MODELS.values()]
        assert statuses == sorted(statuses, key=lambda s: 0 if s == "current" else 1), (
            f"picker order interleaves legacy with current: {statuses}"
        )

    def test_legacy_picker_entries_are_labelled(self):
        """An operator tapping a button must be able to see it is not the pick."""
        from robothor.engine.telegram_handlers import AVAILABLE_MODELS

        for display, model_id in AVAILABLE_MODELS.items():
            if _MODEL_REGISTRY[model_id].status == "legacy":
                assert "legacy" in display.lower(), (
                    f"{display!r} is legacy but nothing in the label says so"
                )
