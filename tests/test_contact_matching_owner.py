"""Owner-priority tiebreak in robothor.memory.contact_matching.find_best_match."""

from __future__ import annotations

from robothor.memory.contact_matching import find_best_match


class TestOwnerTiebreak:
    def test_owner_wins_tiebreak_when_input_identifies_operator(self):
        # Two "Philip"s tie at 0.8 (single name matches part of full name).
        candidates = [
            {"id": "other-philip", "name": "Philip Krupenya"},
            {"id": "owner-philip", "name": "Philip Owner"},
        ]
        result = find_best_match(
            "Philip",
            candidates,
            owner_candidate_id="owner-philip",
            owner_nicknames={"philip", "phil", "owner"},
        )
        assert result is not None
        assert result["id"] == "owner-philip"

    def test_higher_score_beats_owner_tiebreak(self):
        # Owner matches at 0.8, Amurao matches at 0.95 — higher score wins.
        candidates = [
            {"id": "owner-philip", "name": "Philip Owner"},
            {"id": "amurao", "name": "Philip Amurao"},
        ]
        result = find_best_match(
            "Philip Amurao",
            candidates,
            owner_candidate_id="owner-philip",
            owner_nicknames={"philip", "phil"},
        )
        assert result["id"] == "amurao"

    def test_no_owner_context_behaves_as_before(self):
        candidates = [
            {"id": "a", "name": "Philip Owner", "mention_count": 1},
            {"id": "b", "name": "Philip Krupenya", "mention_count": 5},
        ]
        result = find_best_match("Philip", candidates)
        # Higher mention_count wins at tie.
        assert result["id"] == "b"

    def test_owner_wins_over_higher_mention_count_on_tie(self):
        candidates = [
            {"id": "owner-philip", "name": "Philip Owner", "mention_count": 1},
            {"id": "krup", "name": "Philip Krupenya", "mention_count": 99},
        ]
        result = find_best_match(
            "Philip",
            candidates,
            owner_candidate_id="owner-philip",
            owner_nicknames={"philip"},
        )
        assert result["id"] == "owner-philip"

    def test_input_not_an_owner_name_leaves_mention_tiebreak(self):
        candidates = [
            {"id": "owner-philip", "name": "Philip Owner", "mention_count": 1},
            {"id": "krup", "name": "Philip Krupenya", "mention_count": 5},
        ]
        # Input "Krupenya" clearly points at the non-owner; owner priority
        # must not fire just because the owner is in the candidate list.
        result = find_best_match(
            "Krupenya",
            candidates,
            owner_candidate_id="owner-philip",
            owner_nicknames={"philip"},
        )
        assert result["id"] == "krup"

    def test_nickname_triggers_owner_priority(self):
        candidates = [
            {"id": "owner-philip", "name": "Philip Owner"},
            {"id": "other", "name": "Philip Krupenya"},
        ]
        result = find_best_match(
            "Phil",  # nickname, canonicalizes to "philip"
            candidates,
            owner_candidate_id="owner-philip",
            owner_nicknames={"philip"},
        )
        assert result["id"] == "owner-philip"
