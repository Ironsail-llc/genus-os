"""A memory search leg that fails says so.

search_facts appends three best-effort legs (insights, episodes, verbatim chat
turns). Each one used to be `except Exception: pass`, so a broken episode store
returned fewer results and the log said nothing. The search still succeeds;
the failure is now a WARNING naming the leg.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.memory import facts

_LOGGER = "robothor.memory.facts"

_ROW = {
    "id": 1,
    "fact_text": "Alice prefers tea",
    "category": "preference",
    "entities": ["Alice"],
    "confidence": 0.9,
    "source_type": "conversation",
    "metadata": {},
    "created_at": None,
    "importance_score": 0.5,
    "access_count": 0,
    "superseded_by": None,
    "person_id": None,
    "age_seconds": 100.0,
    "bm25_score": 0.4,
}


def _warned(caplog: pytest.LogCaptureFixture, *needles: str) -> bool:
    return any(
        r.name == _LOGGER
        and r.levelno >= logging.WARNING
        and all(n in r.getMessage() for n in needles)
        for r in caplog.records
    )


async def _aux(**include: bool) -> list[dict[str, Any]]:
    return await facts._append_auxiliary(
        [dict(_ROW)],
        query="tea",
        embedding=[0.1] * 8,
        tenant_id="test-tenant",
        _tenant="test-tenant",
        include_insights=include.get("insights", False),
        include_episodes=include.get("episodes", False),
        include_chat_turns=include.get("chat_turns", False),
    )


@pytest.mark.asyncio
async def test_a_failed_insight_leg_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(
            facts, "search_insights", AsyncMock(side_effect=RuntimeError("insights down"))
        ),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        out = await _aux(insights=True)
    assert [r["fact_text"] for r in out] == ["Alice prefers tea"]
    assert _warned(caplog, "insight", "insights down")


@pytest.mark.asyncio
async def test_a_failed_episode_leg_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch(
            "robothor.memory.episodes.search_episodes",
            AsyncMock(side_effect=RuntimeError("episodes down")),
        ),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        out = await _aux(episodes=True)
    assert len(out) == 1
    assert _warned(caplog, "episode", "episodes down")


@pytest.mark.asyncio
async def test_a_failed_chat_turn_leg_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    def _boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("chat store down")

    with (
        patch("robothor.engine.chat_store.search_chat_turns", _boom),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        out = await _aux(chat_turns=True)
    assert len(out) == 1
    assert _warned(caplog, "chat", "chat store down")


@pytest.mark.asyncio
async def test_the_reranked_path_logs_a_failed_insight_leg(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The flag-off reranked branch had its own copy of the insight swallow."""
    monkeypatch.delenv("MEMORY_EPISODE_MERGE", raising=False)
    cur = MagicMock()
    cur.fetchall.return_value = [dict(_ROW)]
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    async def _rerank(query: str, candidates: list[dict[str, Any]], top_k: int) -> Any:
        return candidates

    with (
        patch.object(
            facts.llm_client,
            "get_embedding_async",
            AsyncMock(side_effect=httpx.ReadTimeout("embed timed out")),
        ),
        patch.object(facts, "get_connection", return_value=conn),
        patch.object(facts, "apply_hnsw_session"),
        patch.object(facts, "_episode_merge_enabled", return_value=False),
        patch("robothor.rag.reranker.rerank_with_fallback", _rerank),
        patch.object(
            facts, "search_insights", AsyncMock(side_effect=RuntimeError("insights down"))
        ),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        results = await facts.search_facts(
            "tea",
            limit=5,
            use_reranker=True,
            expand_entities=False,
            include_insights=True,
            include_episodes=False,
        )
    assert [r["fact_text"] for r in results] == ["Alice prefers tea"]
    assert _warned(caplog, "insight", "insights down")
    assert not any("rerank failed" in r.getMessage() for r in caplog.records), (
        "an insight failure must not be mistaken for a reranker failure"
    )


def test_a_failed_lifecycle_lock_release_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.memory import lifecycle

    def _boom() -> Any:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("robothor.config.get_config", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.memory.lifecycle"):
        lifecycle._release_lifecycle_lock()
    assert any(
        r.name == "robothor.memory.lifecycle" and "config unreadable" in r.getMessage()
        for r in caplog.records
    )
