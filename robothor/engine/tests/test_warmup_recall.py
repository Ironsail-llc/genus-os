"""Tests for WS-5 interactive recall surface (`robothor.engine.warmup`).

Covers the case-insensitive candidate extraction that fixes the lowercase
no-op bug. The recency-blended SQL ranking is exercised live (needs a DB).
"""

from __future__ import annotations

from typing import Any

from robothor.engine.warmup import _extract_entity_candidates, _warmup_recall_v2_enabled


class TestCandidates:
    def test_capitalized_only_when_not_lowercase_ok(self) -> None:
        out = _extract_entity_candidates(
            "Did Philip confirm the OpenRouter login was legitimate?", lowercase_ok=False
        )
        assert "Philip" in out and "OpenRouter" in out
        assert "Did" not in out  # capitalized sentence-start stopword
        assert "login" not in out  # lowercase excluded in legacy mode

    def test_lowercase_query_is_empty_without_v2(self) -> None:
        # Reproduces the live no-op: a lowercase query yields zero candidates.
        assert _extract_entity_candidates("is the inwood login ok", lowercase_ok=False) == []

    def test_lowercase_ok_includes_content_words(self) -> None:
        out = _extract_entity_candidates(
            "what is the status of the openrouter login", lowercase_ok=True
        )
        assert "openrouter" in out
        assert "the" not in out and "what" not in out and "is" not in out

    def test_capitalized_first_ordering(self) -> None:
        out = _extract_entity_candidates("the OpenRouter login issue", lowercase_ok=True)
        assert out[0] == "OpenRouter"  # proper nouns rank before lowercase content words

    def test_dedup(self) -> None:
        out = _extract_entity_candidates("OpenRouter OpenRouter openrouter", lowercase_ok=True)
        assert out.count("OpenRouter") == 1
        assert "openrouter" not in out  # already seen (case-insensitive)


class TestFlag:
    def test_default_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_WARMUP_RECALL_V2", raising=False)
        assert _warmup_recall_v2_enabled() is False

    def test_on(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_WARMUP_RECALL_V2", "1")
        assert _warmup_recall_v2_enabled() is True
