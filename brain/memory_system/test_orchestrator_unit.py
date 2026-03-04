"""
Orchestrator Unit Tests — Pure function tests, no LLM or I/O required.

Tests classify_query() and format_merged_context() from orchestrator.py.
These are deterministic functions that don't need mocking.
"""

from orchestrator import CLASSIFICATION_RULES, RAG_PROFILES, classify_query, format_merged_context

# ─── classify_query() ────────────────────────────────────────────────


class TestClassifyQuery:
    """Tests for query classification into RAG profiles."""

    def test_fast_query(self):
        """Simple/quick queries map to 'fast' profile."""
        assert classify_query("what time is it") == "fast"
        assert classify_query("Give me a quick summary") == "fast"

    def test_code_query(self):
        """Programming-related queries map to 'code' profile."""
        assert classify_query("fix the python bug in my script") == "code"
        assert classify_query("Write a function to parse JSON") == "code"

    def test_research_query(self):
        """Deep-dive queries map to 'research' profile."""
        assert classify_query("explain how RAG retrieval augmented generation works") == "research"
        assert classify_query("compare PostgreSQL and MySQL pros and cons") == "research"

    def test_general_query(self):
        """Queries with no keyword match fall back to 'general'."""
        assert classify_query("tell me about cats") == "general"
        assert classify_query("what did Philip do yesterday") == "general"

    def test_expert_query(self):
        """Expert-level queries with analysis keywords map to 'expert'."""
        assert classify_query("comprehensive technical deep dive into memory systems") == "expert"
        assert (
            classify_query("give me an expert thorough analysis with detailed breakdown")
            == "expert"
        )

    def test_heavy_query(self):
        """Exhaustive queries map to 'heavy' profile."""
        assert (
            classify_query("gather all information about this topic, exhaustive search") == "heavy"
        )

    def test_empty_query_returns_general(self):
        """Empty string has no keyword matches, falls back to general."""
        assert classify_query("") == "general"

    def test_case_insensitive(self):
        """Classification is case-insensitive."""
        assert classify_query("WRITE A PYTHON FUNCTION") == "code"
        assert classify_query("QUICK ANSWER PLEASE") == "fast"


# ─── format_merged_context() ─────────────────────────────────────────


class TestFormatMergedContext:
    """Tests for context formatting with memory and web results."""

    def test_memory_first_ordering(self):
        """Memory results appear before web results in output."""
        memory = [
            {"content": "Memory fact 1", "tier": "long", "similarity": 0.9, "content_type": "fact"}
        ]
        web = [{"title": "Web Result", "url": "https://example.com", "content": "Web content"}]

        result = format_merged_context(memory, web)
        mem_pos = result.index("[Memory 1]")
        web_pos = result.index("[Web 1]")
        assert mem_pos < web_pos

    def test_truncation_at_max_chars(self):
        """Output respects max_chars limit."""
        # Create memory results with known sizes
        memory = [
            {"content": "A" * 500, "tier": "long", "similarity": 0.9, "content_type": "fact"},
            {"content": "B" * 500, "tier": "long", "similarity": 0.8, "content_type": "fact"},
            {"content": "C" * 500, "tier": "long", "similarity": 0.7, "content_type": "fact"},
        ]
        web = [{"title": "Big", "url": "https://x.com", "content": "D" * 500}]

        result = format_merged_context(memory, web, max_chars=800)
        # Should be truncated well before including all 4 entries
        assert len(result) <= 900  # some overhead for labels

    def test_empty_results_returns_no_context(self):
        """Empty memory and web results returns fallback message."""
        result = format_merged_context([], [])
        assert result == "No relevant context found."

    def test_memory_only(self):
        """Works with memory results and no web results."""
        memory = [
            {
                "content": "Only memory",
                "tier": "short",
                "similarity": 0.85,
                "content_type": "conversation",
            }
        ]
        result = format_merged_context(memory, [])
        assert "[Memory 1]" in result
        assert "Only memory" in result
        assert "[Web" not in result

    def test_web_only(self):
        """Works with web results and no memory results."""
        web = [{"title": "Page Title", "url": "https://example.com", "content": "Page content"}]
        result = format_merged_context([], web)
        assert "[Web 1]" in result
        assert "Page Title" in result
        assert "[Memory" not in result

    def test_rerank_score_displayed(self):
        """Rerank score appears in the formatted output when present."""
        memory = [
            {
                "content": "Fact",
                "tier": "long",
                "similarity": 0.9,
                "rerank_score": 0.95,
                "content_type": "fact",
            }
        ]
        result = format_merged_context(memory, [])
        assert "rerank=0.950" in result

    def test_rerank_relevant_displayed(self):
        """rerank_relevant boolean appears when present (instead of raw score)."""
        memory = [
            {
                "content": "Fact",
                "tier": "long",
                "similarity": 0.9,
                "rerank_relevant": True,
                "content_type": "fact",
            }
        ]
        result = format_merged_context(memory, [])
        assert "relevant=True" in result

    def test_separator_between_entries(self):
        """Entries are separated by --- dividers."""
        memory = [
            {"content": "Fact 1", "tier": "long", "similarity": 0.9, "content_type": "fact"},
            {"content": "Fact 2", "tier": "long", "similarity": 0.8, "content_type": "fact"},
        ]
        result = format_merged_context(memory, [])
        assert "\n\n---\n\n" in result


# ─── RAG_PROFILES validation ────────────────────────────────────────


class TestRagProfiles:
    """Validate that RAG_PROFILES has required structure."""

    REQUIRED_KEYS = {
        "description",
        "memory_limit",
        "web_limit",
        "rerank_top_k",
        "temperature",
        "max_tokens",
        "use_reranker",
        "use_web",
    }

    def test_all_profiles_have_required_keys(self):
        """Every profile must have all required configuration keys."""
        for name, profile in RAG_PROFILES.items():
            missing = self.REQUIRED_KEYS - set(profile.keys())
            assert not missing, f"Profile '{name}' missing keys: {missing}"

    def test_all_classification_outputs_have_profiles(self):
        """Every category in CLASSIFICATION_RULES has a matching RAG_PROFILES entry."""
        for category in CLASSIFICATION_RULES:
            assert category in RAG_PROFILES, f"Classification '{category}' has no matching profile"

    def test_general_profile_exists(self):
        """The 'general' fallback profile must exist."""
        assert "general" in RAG_PROFILES

    def test_temperature_ranges(self):
        """All temperatures are between 0 and 2."""
        for name, profile in RAG_PROFILES.items():
            assert 0 <= profile["temperature"] <= 2, f"Profile '{name}' has invalid temperature"

    def test_limits_are_positive(self):
        """All limits are positive integers."""
        for name, profile in RAG_PROFILES.items():
            assert profile["memory_limit"] > 0, f"Profile '{name}' has non-positive memory_limit"
            assert profile["web_limit"] > 0, f"Profile '{name}' has non-positive web_limit"
            assert profile["max_tokens"] > 0, f"Profile '{name}' has non-positive max_tokens"
