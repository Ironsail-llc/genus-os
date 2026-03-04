"""Unit tests for contact_matching.py — pure functions, no mocks needed."""

from contact_matching import find_best_match, name_similarity, normalize_name


class TestNormalizeName:
    def test_basic_normalization(self):
        assert normalize_name("  Philip  D'Agostino  ") == "philip d'agostino"

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_none_returns_empty(self):
        assert normalize_name(None) == ""

    def test_unicode_normalization(self):
        assert normalize_name("Café") == "cafe\u0301"

    def test_preserves_apostrophe(self):
        assert normalize_name("D'Agostino") == "d'agostino"


class TestNameSimilarity:
    def test_exact_match(self):
        assert name_similarity("Philip D'Agostino", "Philip D'Agostino") == 1.0

    def test_exact_match_case_insensitive(self):
        assert name_similarity("philip d'agostino", "PHILIP D'AGOSTINO") == 1.0

    def test_first_last_match(self):
        assert name_similarity("Philip D'Agostino", "Philip James D'Agostino") == 0.95

    def test_prefix_match_greg_gregory(self):
        score = name_similarity("Greg Smith", "Gregory Smith")
        assert score >= 0.85, f"Expected >= 0.85, got {score}"

    def test_nickname_match(self):
        score = name_similarity("Greg Smith", "Gregory Smith")
        assert score >= 0.85

    def test_nickname_sam_samantha(self):
        score = name_similarity("Sam D'Agostino", "Samantha D'Agostino")
        assert score >= 0.9

    def test_single_name_vs_full_name(self):
        score = name_similarity("Philip", "Philip D'Agostino")
        assert score == 0.8

    def test_single_name_vs_full_last_name(self):
        score = name_similarity("Rochelle", "Rochelle Blaza")
        assert score == 0.8

    def test_reversed_name(self):
        score = name_similarity("D'Agostino Rizzi", "Rizzi D'Agostino")
        assert score == 0.9

    def test_three_part_names(self):
        score = name_similarity("Jhon Ray Angcon", "Jhon Angcon")
        assert score == 0.95  # first + last match

    def test_no_match_returns_zero(self):
        assert name_similarity("Alice Johnson", "Bob Williams") == 0.0

    def test_empty_strings(self):
        assert name_similarity("", "Philip") == 0.0
        assert name_similarity("Philip", "") == 0.0
        assert name_similarity("", "") == 0.0

    def test_single_name_exact(self):
        assert name_similarity("Philip", "Philip") == 1.0

    def test_different_last_names(self):
        assert name_similarity("Philip Smith", "Philip Jones") == 0.0


class TestFindBestMatch:
    def test_finds_exact_match(self):
        candidates = [
            {"name": "Alice Johnson", "mention_count": 5},
            {"name": "Philip D'Agostino", "mention_count": 82},
            {"name": "Bob Williams", "mention_count": 3},
        ]
        result = find_best_match("Philip D'Agostino", candidates)
        assert result is not None
        assert result["name"] == "Philip D'Agostino"
        assert result["match_score"] == 1.0

    def test_finds_partial_match(self):
        candidates = [
            {"name": "Rochelle Blaza", "mention_count": 10},
            {"name": "Bob Williams", "mention_count": 3},
        ]
        result = find_best_match("Rochelle", candidates)
        assert result is not None
        assert result["name"] == "Rochelle Blaza"
        assert result["match_score"] == 0.8

    def test_no_match_returns_none(self):
        candidates = [
            {"name": "Alice Johnson", "mention_count": 5},
            {"name": "Bob Williams", "mention_count": 3},
        ]
        result = find_best_match("Charlie Brown", candidates)
        assert result is None

    def test_prefers_higher_mention_count(self):
        candidates = [
            {"name": "Philip", "mention_count": 148},
            {"name": "Philip", "mention_count": 5},
        ]
        result = find_best_match("Philip", candidates)
        assert result is not None
        assert result["mention_count"] == 148

    def test_respects_threshold(self):
        candidates = [
            {"name": "Rochelle Blaza", "mention_count": 10},
        ]
        # Default threshold is 0.75 — single name match is 0.8, should pass
        result = find_best_match("Rochelle", candidates, threshold=0.75)
        assert result is not None

        # Higher threshold should filter it out
        result = find_best_match("Rochelle", candidates, threshold=0.85)
        assert result is None

    def test_custom_name_key(self):
        candidates = [
            {"display_name": "Philip D'Agostino", "id": 1},
        ]
        result = find_best_match("Philip D'Agostino", candidates, name_key="display_name")
        assert result is not None
        assert result["id"] == 1

    def test_empty_candidates(self):
        assert find_best_match("Philip", []) is None

    def test_empty_name(self):
        candidates = [{"name": "Alice", "mention_count": 5}]
        assert find_best_match("", candidates) is None

    def test_prefers_higher_score_over_mention_count(self):
        candidates = [
            {"name": "Philip D'Agostino", "mention_count": 5},
            {"name": "Philip", "mention_count": 200},
        ]
        result = find_best_match("Philip D'Agostino", candidates)
        assert result is not None
        # Exact match (1.0) should win over partial match (0.8) regardless of mention_count
        assert result["name"] == "Philip D'Agostino"
        assert result["match_score"] == 1.0
