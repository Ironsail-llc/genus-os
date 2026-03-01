"""Tests for robothor.memory.contact_matching — pure Python, no deps."""

from robothor.memory.contact_matching import (
    find_best_match,
    name_similarity,
    normalize_name,
)


class TestNormalizeName:
    def test_empty(self):
        assert normalize_name("") == ""

    def test_whitespace(self):
        assert normalize_name("  John   Smith  ") == "john smith"

    def test_unicode(self):
        result = normalize_name("Jose\u0301")
        assert result.startswith("jose")

    def test_case(self):
        assert normalize_name("JOHN SMITH") == "john smith"


class TestNameSimilarity:
    def test_exact_match(self):
        assert name_similarity("John Smith", "john smith") == 1.0

    def test_empty(self):
        assert name_similarity("", "John") == 0.0
        assert name_similarity("John", "") == 0.0

    def test_first_last_match(self):
        assert name_similarity("John Smith", "John Michael Smith") == 0.95

    def test_nickname(self):
        assert name_similarity("Greg Smith", "Gregory Smith") == 0.9

    def test_prefix_match(self):
        assert name_similarity("Greg Smith", "Gregory Smith") >= 0.85

    def test_single_part_match(self):
        assert name_similarity("Smith", "John Smith") == 0.8

    def test_no_match(self):
        assert name_similarity("Alice Jones", "Bob Smith") == 0.0

    def test_reversed_order(self):
        assert name_similarity("Smith John", "John Smith") == 0.9

    def test_nickname_single(self):
        assert name_similarity("Mike", "Michael Johnson") == 0.8

    def test_various_nicknames(self):
        pairs = [
            ("Bob", "Robert"),
            ("Bill", "William"),
            ("Jim", "James"),
            ("Sam", "Samantha"),
            ("Dan", "Daniel"),
        ]
        for nick, full in pairs:
            assert name_similarity(f"{nick} Doe", f"{full} Doe") >= 0.85, (
                f"Expected {nick}/{full} Doe to match"
            )


class TestFindBestMatch:
    def test_exact(self):
        candidates = [{"name": "John Smith"}, {"name": "Jane Doe"}]
        result = find_best_match("John Smith", candidates)
        assert result is not None
        assert result["name"] == "John Smith"
        assert result["match_score"] == 1.0

    def test_no_match(self):
        candidates = [{"name": "Alice Jones"}]
        result = find_best_match("Bob Smith", candidates)
        assert result is None

    def test_threshold(self):
        candidates = [{"name": "John Smith"}]
        result = find_best_match("John", candidates, threshold=0.9)
        assert result is None  # 0.8 < 0.9 threshold

    def test_custom_key(self):
        candidates = [{"full_name": "John Smith"}]
        result = find_best_match("John Smith", candidates, name_key="full_name")
        assert result is not None

    def test_empty_inputs(self):
        assert find_best_match("", [{"name": "John"}]) is None
        assert find_best_match("John", []) is None

    def test_tie_breaking_by_mention_count(self):
        candidates = [
            {"name": "John Smith", "mention_count": 5},
            {"name": "John Smith", "mention_count": 10},
        ]
        result = find_best_match("John Smith", candidates)
        assert result is not None
        assert result["mention_count"] == 10
