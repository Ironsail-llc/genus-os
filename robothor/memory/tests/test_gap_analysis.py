"""Tests for knowledge gap analysis (Curiosity Engine support)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.memory.gap_analysis import (
    analyze_knowledge_gaps,
    find_entity_type_imbalances,
    find_low_confidence_facts,
    find_orphaned_entities,
    find_thin_entity_clusters,
    find_uncertainty_signals,
)


@pytest.fixture
def mock_db():
    """Mock get_connection for DB operations."""
    with patch("robothor.memory.gap_analysis.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        yield cur


@pytest.fixture
def mock_engine_db():
    """Mock get_engine_connection for agent_runs DB operations."""
    with patch("robothor.memory.gap_analysis.get_engine_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        yield cur


class TestFindOrphanedEntities:
    @pytest.mark.asyncio
    async def test_returns_single_mention_zero_relation_entities(self, mock_db):
        mock_db.fetchall.return_value = [
            {"id": 1, "name": "Orphan Corp", "entity_type": "organization", "mention_count": 1},
        ]
        result = await find_orphaned_entities()
        assert len(result) == 1
        assert result[0]["name"] == "Orphan Corp"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_orphans(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_orphaned_entities()
        assert result == []


class TestFindLowConfidenceFacts:
    @pytest.mark.asyncio
    async def test_returns_facts_below_threshold(self, mock_db):
        mock_db.fetchall.return_value = [
            {
                "id": 10,
                "fact_text": "Maybe something happened",
                "confidence": 0.3,
                "category": "event",
            },
        ]
        result = await find_low_confidence_facts()
        assert len(result) == 1
        assert result[0]["confidence"] == 0.3

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_confident(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_low_confidence_facts()
        assert result == []


class TestFindEntityTypeImbalances:
    @pytest.mark.asyncio
    async def test_detects_imbalanced_types(self, mock_db):
        mock_db.fetchall.return_value = [
            {"entity_type": "person", "count": 50},
            {"entity_type": "organization", "count": 2},
            {"entity_type": "technology", "count": 30},
            {"entity_type": "location", "count": 1},
        ]
        result = await find_entity_type_imbalances()
        imbalanced_types = [r["entity_type"] for r in result]
        assert "organization" in imbalanced_types
        assert "location" in imbalanced_types
        assert "person" not in imbalanced_types

    @pytest.mark.asyncio
    async def test_returns_empty_when_balanced(self, mock_db):
        mock_db.fetchall.return_value = [
            {"entity_type": "person", "count": 10},
            {"entity_type": "organization", "count": 8},
        ]
        result = await find_entity_type_imbalances()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_entities(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_entity_type_imbalances()
        assert result == []


class TestFindThinEntityClusters:
    @pytest.mark.asyncio
    async def test_returns_well_mentioned_but_poorly_connected(self, mock_db):
        mock_db.fetchall.return_value = [
            {
                "id": 5,
                "name": "Acme Corp",
                "entity_type": "organization",
                "mention_count": 10,
                "relation_count": 1,
            },
        ]
        result = await find_thin_entity_clusters()
        assert len(result) == 1
        assert result[0]["name"] == "Acme Corp"

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_well_connected(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_thin_entity_clusters()
        assert result == []


class TestFindUncertaintySignals:
    @pytest.mark.asyncio
    async def test_returns_uncertain_agent_outputs(self, mock_engine_db):
        mock_engine_db.fetchall.return_value = [
            {
                "agent_id": "main",
                "output_snippet": "I don't have enough information about...",
                "created_at": "2026-04-12",
            },
        ]
        result = await find_uncertainty_signals()
        assert len(result) == 1
        assert result[0]["agent_id"] == "main"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_uncertainty(self, mock_engine_db):
        mock_engine_db.fetchall.return_value = []
        result = await find_uncertainty_signals()
        assert result == []


class TestAnalyzeKnowledgeGaps:
    @pytest.mark.asyncio
    async def test_returns_all_gap_categories(self):
        with (
            patch("robothor.memory.gap_analysis.find_orphaned_entities", return_value=[]),
            patch("robothor.memory.gap_analysis.find_low_confidence_facts", return_value=[]),
            patch("robothor.memory.gap_analysis.find_entity_type_imbalances", return_value=[]),
            patch("robothor.memory.gap_analysis.find_uncertainty_signals", return_value=[]),
            patch("robothor.memory.gap_analysis.find_thin_entity_clusters", return_value=[]),
        ):
            result = await analyze_knowledge_gaps()
            assert "orphaned_entities" in result
            assert "low_confidence_facts" in result
            assert "type_imbalances" in result
            assert "uncertainty_signals" in result
            assert "thin_clusters" in result
