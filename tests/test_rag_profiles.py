"""Tests for robothor.rag.profiles — query classification and profile config."""

from robothor.rag.profiles import (
    CLASSIFICATION_RULES,
    PROFILE_REQUIRED_KEYS,
    RAG_PROFILES,
    classify_query,
)


class TestRagProfiles:
    """Validate RAG_PROFILES structure."""

    def test_all_profiles_have_required_keys(self):
        for name, profile in RAG_PROFILES.items():
            missing = PROFILE_REQUIRED_KEYS - set(profile.keys())
            assert not missing, f"Profile '{name}' missing keys: {missing}"

    def test_all_classification_outputs_have_profiles(self):
        """Every category in CLASSIFICATION_RULES has a matching profile."""
        for category in CLASSIFICATION_RULES:
            assert category in RAG_PROFILES, f"Classification '{category}' has no matching profile"

    def test_general_profile_exists(self):
        assert "general" in RAG_PROFILES

    def test_expected_profiles(self):
        expected = {"fast", "general", "research", "code", "expert", "heavy"}
        assert expected.issubset(set(RAG_PROFILES.keys()))

    def test_temperature_ranges(self):
        for name, profile in RAG_PROFILES.items():
            assert 0 <= profile["temperature"] <= 2, f"Profile '{name}' has invalid temperature"

    def test_limits_are_positive(self):
        for name, profile in RAG_PROFILES.items():
            assert profile["memory_limit"] > 0, f"Profile '{name}' has non-positive memory_limit"
            assert profile["web_limit"] > 0, f"Profile '{name}' has non-positive web_limit"
            assert profile["max_tokens"] > 0, f"Profile '{name}' has non-positive max_tokens"

    def test_rerank_top_k_positive(self):
        for name, profile in RAG_PROFILES.items():
            assert profile["rerank_top_k"] > 0, f"Profile '{name}' has non-positive rerank_top_k"


class TestClassifyQuery:
    def test_code_query(self):
        assert classify_query("How do I fix this Python bug?") == "code"

    def test_research_query(self):
        assert classify_query("Explain in detail how transformers work") == "research"

    def test_fast_query(self):
        assert classify_query("What time is it? Quick answer") == "fast"

    def test_expert_query(self):
        assert classify_query("Give me a comprehensive thorough analysis") == "expert"

    def test_heavy_query(self):
        assert classify_query("Tell me everything about this topic, exhaustive") == "heavy"

    def test_general_fallback(self):
        assert classify_query("Hello, how are you today?") == "general"

    def test_empty_query(self):
        assert classify_query("") == "general"

    def test_mixed_signals(self):
        """When multiple profiles match, the one with more keyword hits wins."""
        result = classify_query("debug this python function code error traceback")
        assert result == "code"  # Multiple code keywords

    def test_case_insensitive(self):
        assert classify_query("QUICK SUMMARY PLEASE") == "fast"
