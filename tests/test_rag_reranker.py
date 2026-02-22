"""Tests for robothor.rag.reranker — prompt building and structure (pure unit tests)."""

from robothor.rag.reranker import RERANKER_MODEL, build_reranker_prompt


class TestBuildRerankerPrompt:
    def test_contains_query(self):
        prompt = build_reranker_prompt("test query", "test document")
        assert "test query" in prompt

    def test_contains_document(self):
        prompt = build_reranker_prompt("query", "document text here")
        assert "document text here" in prompt

    def test_chatml_format(self):
        prompt = build_reranker_prompt("q", "d")
        assert "<|im_start|>system" in prompt
        assert "<|im_end|>" in prompt
        assert "<|im_start|>user" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_think_tags_prefilled(self):
        """Pre-filled think tags skip reasoning for direct yes/no output."""
        prompt = build_reranker_prompt("q", "d")
        assert "<think>\n\n</think>" in prompt

    def test_yes_no_instruction(self):
        prompt = build_reranker_prompt("q", "d")
        assert '"yes"' in prompt or "yes" in prompt.lower()
        assert '"no"' in prompt or "no" in prompt.lower()

    def test_document_truncated(self):
        long_doc = "x" * 5000
        prompt = build_reranker_prompt("q", long_doc)
        # Document should be truncated to 3000 chars
        assert "x" * 3001 not in prompt

    def test_custom_instruction(self):
        prompt = build_reranker_prompt("q", "d", instruction="Custom instruction")
        assert "Custom instruction" in prompt


class TestRerankerModel:
    def test_model_is_string(self):
        assert isinstance(RERANKER_MODEL, str)

    def test_model_has_reranker(self):
        assert "reranker" in RERANKER_MODEL.lower() or "Reranker" in RERANKER_MODEL
