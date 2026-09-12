"""The vault key names are spelled in exactly one place.

Three components write provider credentials — the setup wizard, the Settings
page, and the engine's reload — and a fourth (``export_env``) derives an
environment variable name from whatever string they agreed on. A one-character
disagreement there is not a crash: it is a key the operator believes is
configured and the engine never reads. These tests pin the spelling.
"""

from __future__ import annotations

import pytest

from robothor.vault.naming import channel_field, provider_key, validate_component


class TestProviderKey:
    def test_slot_one_has_no_suffix(self) -> None:
        assert provider_key("openrouter") == "providers/openrouter/api_key"

    def test_default_position_is_slot_one(self) -> None:
        assert provider_key("openai", 1) == provider_key("openai")

    def test_spares_are_numbered_from_two(self) -> None:
        assert provider_key("openrouter", 2) == "providers/openrouter/api_key_2"
        assert provider_key("openrouter", 7) == "providers/openrouter/api_key_7"

    def test_ids_are_normalised_to_lower_case(self) -> None:
        """``export_env`` upper-cases every key, so two cases would collide."""
        assert provider_key("OpenRouter") == "providers/openrouter/api_key"

    @pytest.mark.parametrize("position", [0, -1])
    def test_position_below_one_is_rejected(self, position: int) -> None:
        with pytest.raises(ValueError, match="position"):
            provider_key("openrouter", position)


class TestChannelField:
    def test_shape(self) -> None:
        assert channel_field("telegram", "bot_token") == "channels/telegram/bot_token"

    def test_components_are_normalised(self) -> None:
        assert channel_field("Slack", "Bot_Token") == "channels/slack/bot_token"


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "open router", "open\trouter", "open\nrouter", "a/b", "/openrouter"],
    )
    def test_separators_and_whitespace_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_component(bad, what="provider id")

    @pytest.mark.parametrize("bad", ["", "   ", "a/b", "x y"])
    def test_provider_key_rejects_the_same_ids(self, bad: str) -> None:
        with pytest.raises(ValueError):
            provider_key(bad)

    @pytest.mark.parametrize("bad", ["", "a/b", "x y"])
    def test_channel_field_rejects_bad_components(self, bad: str) -> None:
        with pytest.raises(ValueError):
            channel_field(bad, "token")
        with pytest.raises(ValueError):
            channel_field("telegram", bad)

    def test_a_valid_component_is_returned_normalised(self) -> None:
        assert validate_component("  OpenRouter  ", what="provider id") == "openrouter"
