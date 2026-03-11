"""Tests for cross-domain insight discovery (Memory System v4.2 P1)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory.lifecycle import (
    _find_similar_insight,
    discover_cross_domain_insights,
    store_insight,
)


@pytest.fixture
def mock_db():
    """Mock get_connection for DB operations."""
    with patch("robothor.memory.lifecycle.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        yield cur


def _make_facts(n: int, categories: list[str] | None = None) -> list[dict]:
    """Helper to create mock fact rows."""
    cats = categories or ["project", "personal", "technical"]
    return [
        {
            "id": i + 1,
            "fact_text": f"Test fact number {i + 1} about something specific",
            "category": cats[i % len(cats)],
            "entities": [f"Entity{i + 1}"],
        }
        for i in range(n)
    ]


class TestDiscoverRequiresMinimumFacts:
    @pytest.mark.asyncio
    async def test_returns_empty_for_few_facts(self, mock_db):
        mock_db.fetchall.return_value = _make_facts(2)
        result = await discover_cross_domain_insights(hours_back=24)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_zero_facts(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await discover_cross_domain_insights(hours_back=24)
        assert result == []


class TestDiscoverRequiresCategoryDiversity:
    @pytest.mark.asyncio
    async def test_returns_empty_for_single_category(self, mock_db):
        facts = _make_facts(5, categories=["project"])
        mock_db.fetchall.return_value = facts
        result = await discover_cross_domain_insights(hours_back=24)
        assert result == []


class TestDiscoverValidatesFactIds:
    @pytest.mark.asyncio
    async def test_filters_invalid_fact_ids(self):
        facts = _make_facts(5, categories=["project", "personal", "technical"])

        llm_response = '{"insights": [{"insight_text": "A valid cross-domain insight about connections", "source_fact_ids": [1, 999]}]}'

        with (
            patch("robothor.memory.lifecycle.get_connection") as mock_conn,
            patch(
                "robothor.memory.lifecycle.llm_client.generate",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn
            cur.fetchall.return_value = facts

            result = await discover_cross_domain_insights(hours_back=24)
            # Should reject — only 1 valid ID (fact 1), 999 is invalid
            assert len(result) == 0


class TestDiscoverRejectsShortInsights:
    @pytest.mark.asyncio
    async def test_filters_short_insights(self):
        facts = _make_facts(5, categories=["project", "personal", "technical"])

        llm_response = '{"insights": [{"insight_text": "Too short", "source_fact_ids": [1, 2]}]}'

        with (
            patch("robothor.memory.lifecycle.get_connection") as mock_conn,
            patch(
                "robothor.memory.lifecycle.llm_client.generate",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn
            cur.fetchall.return_value = facts

            result = await discover_cross_domain_insights(hours_back=24)
            assert len(result) == 0


class TestStoreInsightCreatesEmbedding:
    @pytest.mark.asyncio
    async def test_embeds_and_stores(self):
        fake_embedding = [0.1] * 1024

        with (
            patch(
                "robothor.memory.lifecycle.llm_client.get_embedding_async",
                new_callable=AsyncMock,
                side_effect=[False, fake_embedding],  # _find_similar returns False, then embed
            ),
            patch(
                "robothor.memory.lifecycle._find_similar_insight",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("robothor.memory.lifecycle.get_connection") as mock_conn,
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            # First call returns source fact metadata, second returns insert ID
            cur.fetchall.return_value = [
                {"category": "project", "entities": ["Robothor"]},
                {"category": "personal", "entities": ["Philip"]},
            ]
            cur.fetchone.return_value = (42,)

            insight = {
                "insight_text": "Cross-domain connection between project work and personal life",
                "source_fact_ids": [1, 2],
            }
            result = await store_insight(insight)
            assert result == 42


class TestSimilarInsightDedup:
    @pytest.mark.asyncio
    async def test_dedup_finds_similar(self):
        fake_embedding = [0.1] * 1024

        with (
            patch(
                "robothor.memory.lifecycle.llm_client.get_embedding_async",
                new_callable=AsyncMock,
                return_value=fake_embedding,
            ),
            patch("robothor.memory.lifecycle.get_connection") as mock_conn,
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            # Simulate: 1 similar insight found
            cur.fetchone.return_value = (1,)
            assert await _find_similar_insight("some insight text") is True

            # Simulate: 0 similar insights
            cur.fetchone.return_value = (0,)
            assert await _find_similar_insight("unique insight text") is False


class TestSearchInsights:
    @pytest.mark.asyncio
    async def test_vector_search_returns_results(self):
        from robothor.memory.facts import search_insights

        fake_embedding = [0.1] * 1024
        mock_rows = [
            {
                "id": 1,
                "insight_text": "Cross-domain insight about patterns",
                "source_fact_ids": [10, 20],
                "categories": ["project", "personal"],
                "entities": ["Robothor"],
                "created_at": datetime.now(UTC),
                "metadata": {},
                "similarity": 0.92,
            }
        ]

        with (
            patch(
                "robothor.memory.facts.llm_client.get_embedding_async",
                new_callable=AsyncMock,
                return_value=fake_embedding,
            ),
            patch("robothor.memory.facts.get_connection") as mock_conn,
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn
            cur.fetchall.return_value = mock_rows

            results = await search_insights("pattern recognition", limit=5)
            assert len(results) == 1
            assert results[0]["source"] == "insight"
            assert results[0]["insight_text"] == "Cross-domain insight about patterns"
