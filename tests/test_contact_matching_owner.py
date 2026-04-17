"""Owner-priority tiebreak in robothor.memory.contact_matching.find_best_match."""

from __future__ import annotations

from robothor.memory.contact_matching import find_best_match


class TestOwnerTiebreak:
    def test_owner_wins_tiebreak_when_input_identifies_operator(self):
        # Two "Alice"s tie at 0.8 (single name matches part of full name).
        candidates = [
            {"id": "other-alice", "name": "Alice Example"},
            {"id": "owner-alice", "name": "Alice Owner"},
        ]
        result = find_best_match(
            "Alice",
            candidates,
            owner_candidate_id="owner-alice",
            owner_nicknames={"alice", "ali", "owner"},
        )
        assert result is not None
        assert result["id"] == "owner-alice"

    def test_higher_score_beats_owner_tiebreak(self):
        # Exact-full-name match (1.0) beats owner partial match — owner priority
        # is tiebreak-only, never a score override.
        candidates = [
            {"id": "owner-alice", "name": "Alice Owner"},
            {"id": "other", "name": "Alice Example"},
        ]
        result = find_best_match(
            "Alice Example",
            candidates,
            owner_candidate_id="owner-alice",
            owner_nicknames={"alice", "ali"},
        )
        assert result["id"] == "other"

    def test_no_owner_context_behaves_as_before(self):
        candidates = [
            {"id": "a", "name": "Alice Owner", "mention_count": 1},
            {"id": "b", "name": "Alice Example", "mention_count": 5},
        ]
        result = find_best_match("Alice", candidates)
        # Higher mention_count wins at tie.
        assert result["id"] == "b"

    def test_owner_wins_over_higher_mention_count_on_tie(self):
        candidates = [
            {"id": "owner-alice", "name": "Alice Owner", "mention_count": 1},
            {"id": "other", "name": "Alice Example", "mention_count": 99},
        ]
        result = find_best_match(
            "Alice",
            candidates,
            owner_candidate_id="owner-alice",
            owner_nicknames={"alice"},
        )
        assert result["id"] == "owner-alice"

    def test_input_not_an_owner_name_leaves_mention_tiebreak(self):
        candidates = [
            {"id": "owner-alice", "name": "Alice Owner", "mention_count": 1},
            {"id": "other", "name": "Alice Example", "mention_count": 5},
        ]
        # Input "Example" clearly points at the non-owner; owner priority
        # must not fire just because the owner is in the candidate list.
        result = find_best_match(
            "Example",
            candidates,
            owner_candidate_id="owner-alice",
            owner_nicknames={"alice"},
        )
        assert result["id"] == "other"

    def test_nickname_triggers_owner_priority(self):
        candidates = [
            {"id": "owner-alice", "name": "Alice Owner"},
            {"id": "other", "name": "Alice Example"},
        ]
        result = find_best_match(
            "Ali",  # nickname, canonicalizes to "alice" via owner_nicknames
            candidates,
            owner_candidate_id="owner-alice",
            owner_nicknames={"alice", "ali"},
        )
        assert result["id"] == "owner-alice"

    def test_integer_ids_from_db_tiebreak_correctly(self):
        """Regression for int/str mismatch: memory_entities.id is SERIAL (int),
        so periodic_analysis passes an int into owner_candidate_id. The older
        `str()` cast turned this into "42" while candidates kept int ids, and
        the equality check silently failed."""
        candidates = [
            {"id": 42, "name": "Jordan Owner", "mention_count": 1},
            {"id": 99, "name": "Jordan Example", "mention_count": 99},
        ]
        result = find_best_match(
            "Jordan",
            candidates,
            owner_candidate_id=42,
            owner_nicknames={"jordan"},
        )
        assert result["id"] == 42
