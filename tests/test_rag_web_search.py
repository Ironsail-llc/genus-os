"""Tests for robothor.rag.web_search — format helpers (pure unit tests)."""

from robothor.rag.web_search import format_web_results, web_results_to_memory_format


class TestFormatWebResults:
    def test_empty_results(self):
        assert format_web_results([]) == "No web results found."

    def test_single_result(self):
        results = [
            {"title": "Test Page", "url": "https://example.com", "content": "Page content"}
        ]
        formatted = format_web_results(results)
        assert "[Web 1]" in formatted
        assert "Test Page" in formatted
        assert "https://example.com" in formatted
        assert "Page content" in formatted

    def test_max_chars(self):
        results = [
            {"title": f"Page {i}", "url": f"https://example.com/{i}", "content": "x" * 500}
            for i in range(20)
        ]
        formatted = format_web_results(results, max_chars=500)
        assert len(formatted) < 1000


class TestWebResultsToMemoryFormat:
    def test_conversion(self):
        web = [
            {"title": "Article", "url": "https://ex.com", "content": "Body", "source": "google", "score": 0.8}
        ]
        converted = web_results_to_memory_format(web)
        assert len(converted) == 1
        assert converted[0]["tier"] == "web"
        assert converted[0]["content_type"] == "web_search"
        assert converted[0]["similarity"] == 0.8
        assert converted[0]["metadata"]["url"] == "https://ex.com"

    def test_score_capped_at_one(self):
        web = [{"title": "T", "url": "u", "content": "c", "source": "s", "score": 5.0}]
        converted = web_results_to_memory_format(web)
        assert converted[0]["similarity"] == 1.0

    def test_empty_input(self):
        assert web_results_to_memory_format([]) == []

    def test_content_combines_title_and_body(self):
        web = [{"title": "Title", "url": "u", "content": "Body text", "source": "s", "score": 0.5}]
        converted = web_results_to_memory_format(web)
        assert "Title" in converted[0]["content"]
        assert "Body text" in converted[0]["content"]
