"""Tests for the WS-3 write-time dedup + ingestion-churn guards.

Covers facts._insert_fact ON CONFLICT handling, the conflicts reinforce-not-fork
path, and the conversation-ingest generated-briefing skip. All mocked — no DB.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import robothor.memory.conflicts as cf
import robothor.memory.conversation_ingest as ci
import robothor.memory.facts as facts

_PARAMS = ("text", "event", [], 1.0, "src", "conversation", [0.1], "{}", "tenant", "h")


class TestInsertFact:
    def test_plain_insert_when_flag_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_WRITE_DEDUP", raising=False)
        cur = MagicMock()
        cur.fetchone.return_value = (123,)
        fid = facts._insert_fact(cur, _PARAMS, tenant_id="tenant", content_hash="h")
        assert fid == 123
        assert "ON CONFLICT" not in cur.execute.call_args_list[0].args[0]

    def test_on_conflict_returns_inserted_id(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_WRITE_DEDUP", "1")
        cur = MagicMock()
        cur.fetchone.return_value = (200,)  # the INSERT returned a fresh id
        fid = facts._insert_fact(cur, _PARAMS, tenant_id="tenant", content_hash="h")
        assert fid == 200
        assert "ON CONFLICT" in cur.execute.call_args_list[0].args[0]
        assert cur.execute.call_count == 1  # no fallback SELECT needed

    def test_on_conflict_returns_existing_id(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_WRITE_DEDUP", "1")
        cur = MagicMock()
        cur.fetchone.side_effect = [None, (55,)]  # INSERT did nothing → SELECT existing
        fid = facts._insert_fact(cur, _PARAMS, tenant_id="tenant", content_hash="h")
        assert fid == 55
        assert cur.execute.call_count == 2


class TestReinforceNotFork:
    @pytest.mark.asyncio
    async def test_reinforces_high_similarity_same_category(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_WRITE_DEDUP", "1")
        fact = {"fact_text": "X happened", "category": "event", "entities": [], "confidence": 1.0}
        similar = [{"id": 42, "fact_text": "X occurred", "category": "event", "similarity": 0.95}]
        monkeypatch.setattr(cf, "find_similar_facts", AsyncMock(return_value=similar))
        seen: dict[str, int] = {}
        monkeypatch.setattr(cf, "_reinforce_fact", lambda fid, **_k: seen.update(id=fid))
        store = AsyncMock()
        classify = AsyncMock()
        monkeypatch.setattr(cf, "store_fact", store)
        monkeypatch.setattr(cf, "classify_relationship", classify)

        out = await cf.resolve_and_store(fact, "src", "conversation")
        assert out["action"] == "reinforced"
        assert out["existing_id"] == 42
        assert seen["id"] == 42
        store.assert_not_awaited()  # no new row
        classify.assert_not_awaited()  # skipped the LLM classify

    @pytest.mark.asyncio
    async def test_no_reinforce_when_flag_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_WRITE_DEDUP", raising=False)
        fact = {"fact_text": "X happened", "category": "event", "entities": [], "confidence": 1.0}
        similar = [{"id": 42, "fact_text": "X occurred", "category": "event", "similarity": 0.95}]
        monkeypatch.setattr(cf, "find_similar_facts", AsyncMock(return_value=similar))
        monkeypatch.setattr(
            cf,
            "classify_relationship",
            AsyncMock(return_value={"classification": "new", "reasoning": ""}),
        )
        monkeypatch.setattr(cf, "store_fact", AsyncMock(return_value=7))
        out = await cf.resolve_and_store(fact, "src", "conversation")
        assert out["action"] == "stored"  # falls through to the classifier path


class TestGeneratedBriefingSkip:
    def test_detects_briefing(self) -> None:
        assert ci._is_generated_briefing("Morning Briefing — May 30\n...") is True
        assert ci._is_generated_briefing("Evening Wind-Down summary") is True
        assert ci._is_generated_briefing("hey, can you check this email?") is False

    def test_format_transcript_drops_generated_assistant_turn(self) -> None:
        history = [
            {"role": "user", "content": "morning"},
            {"role": "assistant", "content": "Morning Briefing: an OpenRouter login was detected"},
            {"role": "user", "content": "thanks"},
        ]
        out = ci.format_transcript(history, skip_generated=True)
        assert "Morning Briefing" not in out
        assert "morning" in out and "thanks" in out

    def test_format_transcript_keeps_everything_by_default(self) -> None:
        history = [
            {"role": "user", "content": "morning"},
            {"role": "assistant", "content": "Morning Briefing: stuff"},
        ]
        out = ci.format_transcript(history)
        assert "Morning Briefing" in out
