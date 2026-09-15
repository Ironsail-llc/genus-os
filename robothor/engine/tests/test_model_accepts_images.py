"""Whether a model can SEE is a fact the registry has to carry.

``view_image`` returned image content blocks for every model, and
``llm_client._call_with_image_fallback`` stripped them again when the provider
refused — silently, one layer down. The agent was told it had looked at the
picture and then shown a note where the picture used to be. This is the
registry half: a declared capability, a one-time runtime discovery that can
only ever REVOKE it, and a tri-state answer so "we have never checked" is not
reported as "it cannot see".
"""

from __future__ import annotations

import pytest

from robothor.engine import model_registry as mr


@pytest.fixture(autouse=True)
def _forget_discoveries():
    mr.reset_image_discoveries()
    yield
    mr.reset_image_discoveries()


class TestDeclaredCapability:
    def test_limits_carry_the_flag(self) -> None:
        assert hasattr(mr.ModelLimits(1, 1, 1, 0.0, 0.0), "accepts_images")

    def test_the_default_is_false(self) -> None:
        """Unverifiable offline, so the honest default is 'no'."""
        assert mr.ModelLimits(1, 1, 1, 0.0, 0.0).accepts_images is False

    def test_a_multimodal_fleet_model_is_marked_so(self) -> None:
        assert mr.get_model_limits("openrouter/anthropic/claude-sonnet-4.6").accepts_images is True

    def test_a_local_text_model_is_not(self) -> None:
        assert mr.get_model_limits("ollama_chat/qwen3:8b").accepts_images is False


class TestImageCapability:
    def test_a_registered_multimodal_model_accepts(self) -> None:
        assert mr.image_capability("openrouter/anthropic/claude-opus-4.7") == "accepts"

    def test_a_registered_text_model_rejects(self) -> None:
        assert mr.image_capability("ollama_chat/qwen3:8b") == "rejects"

    def test_a_model_nobody_declared_is_unknown_not_rejecting(self) -> None:
        assert mr.image_capability("acme/never-heard-of-it-v9") == "unknown"

    def test_an_empty_model_is_unknown(self) -> None:
        assert mr.image_capability("") == "unknown"


class TestRuntimeDiscovery:
    def test_a_rejection_is_remembered_for_the_process(self) -> None:
        mr.note_image_rejection("acme/never-heard-of-it-v9")
        assert mr.image_capability("acme/never-heard-of-it-v9") == "rejects"

    def test_discovery_overrides_an_optimistic_declaration(self) -> None:
        """Discovery may only ever REVOKE. A model the registry claims can see,
        that the provider then refuses, is a rejecting model from then on."""
        assert mr.image_capability("openrouter/anthropic/claude-sonnet-4.6") == "accepts"
        mr.note_image_rejection("openrouter/anthropic/claude-sonnet-4.6")
        assert mr.image_capability("openrouter/anthropic/claude-sonnet-4.6") == "rejects"

    def test_the_response_form_of_an_id_matches_the_request_form(self) -> None:
        mr.note_image_rejection("xiaomi/mimo-v2.5-20260422")
        assert mr.image_capability("openrouter/xiaomi/mimo-v2.5") == "rejects"


class TestActiveModel:
    def test_unset_is_empty_rather_than_a_guess(self) -> None:
        assert isinstance(mr.active_model(), str)

    def test_what_was_noted_comes_back(self) -> None:
        token = mr.note_active_model("openrouter/z-ai/glm-5")
        try:
            assert mr.active_model() == "openrouter/z-ai/glm-5"
        finally:
            mr.reset_active_model(token)
