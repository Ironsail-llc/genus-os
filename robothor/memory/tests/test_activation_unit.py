"""Seed linking and the activation module's contract, without a database."""

from __future__ import annotations

import pytest

from robothor.memory import activation


class TestPhrases:
    def test_a_single_token_is_never_a_candidate(self) -> None:
        assert activation.candidate_phrases("Tell Alice about it") == [
            "tell alice",
            "alice about",
            "about it",
            "tell alice about",
            "alice about it",
            "tell alice about it",
        ]
        assert activation.candidate_phrases("Alice") == []
        assert activation.candidate_phrases("Alice?!") == []

    def test_phrases_are_lowercased_and_stripped_of_punctuation(self) -> None:
        phrases = activation.candidate_phrases("Did Alice Smith call, ACME Logistics?")
        assert "alice smith" in phrases
        assert "acme logistics" in phrases
        assert all(p == p.lower() for p in phrases)

    def test_phrases_are_bounded(self) -> None:
        long = " ".join(f"word{i}" for i in range(500))
        phrases = activation.candidate_phrases(long)
        assert len(phrases) <= activation.MAX_PHRASES
        assert all(2 <= len(p.split()) <= activation.MAX_NAME_TOKENS for p in phrases)


class TestStrongKeys:
    def test_emails_are_extracted_lowercased(self) -> None:
        assert activation.strong_keys("mail Alice@Example.com or bob@example.com.") == [
            "alice@example.com",
            "bob@example.com",
        ]

    def test_handles_are_extracted(self) -> None:
        assert "@alice_smith" in activation.strong_keys("ping @alice_smith on chat")

    def test_plain_words_are_not_strong_keys(self) -> None:
        assert activation.strong_keys("Alice Smith said hello") == []


class TestConstants:
    def test_the_tunable_bounds_are_named(self) -> None:
        assert activation.MAX_HOPS == 2
        assert activation.HARD_MAX_HOPS == 3
        assert activation.FANOUT >= 1
        assert 0 < activation.DAMPING < 1
        assert activation.FACTS_PER_ENTITY >= 1


class TestNoSeeds:
    def test_activate_with_no_seeds_returns_nothing(self) -> None:
        def _no_db(*_a: object, **_k: object) -> object:
            raise AssertionError("no seeds must not reach the database")

        assert activation.activate("test-tenant", [], trusted_only=True, connect=_no_db) == []

    def test_seeds_from_empty_text_returns_nothing(self) -> None:
        def _no_db(*_a: object, **_k: object) -> object:
            raise AssertionError("nothing to link must not reach the database")

        assert activation.seeds_from_text("test-tenant", "  ", connect=_no_db) == []


@pytest.mark.parametrize("hops", [-1, 0, 1, 2, 3, 10])
def test_hops_are_clamped_before_the_call(hops: int) -> None:
    assert 0 <= activation.clamp_hops(hops) <= activation.HARD_MAX_HOPS
