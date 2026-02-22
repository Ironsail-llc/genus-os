"""Tests for robothor.rag.context — context formatting (pure unit tests)."""

from robothor.rag.context import SYSTEM_PROMPT, format_context, format_merged_context


class TestFormatContext:
    def test_empty_results(self):
        assert format_context([]) == "No relevant memories found."

    def test_single_result(self):
        results = [
            {
                "content": "Test memory content",
                "tier": "long_term",
                "similarity": 0.85,
                "content_type": "email",
                "created_at": "2024-01-15T10:00:00",
            }
        ]
        ctx = format_context(results)
        assert "[Memory 1]" in ctx
        assert "long_term" in ctx
        assert "email" in ctx
        assert "0.850" in ctx
        assert "Test memory content" in ctx

    def test_multiple_results(self):
        results = [
            {"content": f"Memory {i}", "tier": "short_term", "similarity": 0.5, "content_type": "note"}
            for i in range(3)
        ]
        ctx = format_context(results)
        assert "[Memory 1]" in ctx
        assert "[Memory 2]" in ctx
        assert "[Memory 3]" in ctx
        assert "---" in ctx  # separator

    def test_max_chars_truncation(self):
        results = [
            {"content": "x" * 1000, "tier": "long_term", "similarity": 0.9, "content_type": "note"}
            for _ in range(10)
        ]
        ctx = format_context(results, max_chars=500)
        assert len(ctx) <= 600  # Some slack for metadata

    def test_missing_fields_handled(self):
        """Results with missing fields should still format without error."""
        results = [{"content": "Just content"}]
        ctx = format_context(results)
        assert "Just content" in ctx
        assert "unknown" in ctx  # default tier


class TestFormatMergedContext:
    def test_empty_both(self):
        assert format_merged_context([], []) == "No relevant context found."

    def test_memory_only(self):
        memory = [{"content": "Memory fact", "tier": "long_term", "similarity": 0.8, "content_type": "note"}]
        ctx = format_merged_context(memory, [])
        assert "[Memory 1]" in ctx
        assert "Memory fact" in ctx

    def test_web_only(self):
        web = [{"title": "Wikipedia", "url": "https://en.wikipedia.org", "content": "Article text"}]
        ctx = format_merged_context([], web)
        assert "[Web 1]" in ctx
        assert "Wikipedia" in ctx
        assert "https://en.wikipedia.org" in ctx

    def test_combined(self):
        memory = [{"content": "Memory fact", "tier": "long_term", "similarity": 0.8, "content_type": "note"}]
        web = [{"title": "Result", "url": "https://example.com", "content": "Web content"}]
        ctx = format_merged_context(memory, web)
        assert "[Memory 1]" in ctx
        assert "[Web 1]" in ctx

    def test_rerank_relevant_shown(self):
        memory = [
            {
                "content": "Fact",
                "tier": "long_term",
                "similarity": 0.9,
                "content_type": "note",
                "rerank_relevant": "yes",
            }
        ]
        ctx = format_merged_context(memory, [])
        assert "relevant=yes" in ctx


class TestSystemPrompt:
    def test_system_prompt_exists(self):
        assert len(SYSTEM_PROMPT) > 50

    def test_system_prompt_mentions_context(self):
        assert "context" in SYSTEM_PROMPT.lower()
