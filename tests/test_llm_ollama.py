"""Tests for robothor.llm.ollama — config and module-level constants."""

from robothor.llm.ollama import (
    GENERATION_MODEL,
    GENERATION_MODEL_PREFERENCES,
)


class TestModelConfig:
    def test_default_generation_model(self):
        assert GENERATION_MODEL is not None
        assert isinstance(GENERATION_MODEL, str)

    def test_model_preferences_ordered(self):
        assert len(GENERATION_MODEL_PREFERENCES) >= 2
        # First preference should be a known local generation model
        first = GENERATION_MODEL_PREFERENCES[0]
        assert isinstance(first, str) and len(first) > 0

    def test_all_preferences_are_strings(self):
        for pref in GENERATION_MODEL_PREFERENCES:
            assert isinstance(pref, str)
