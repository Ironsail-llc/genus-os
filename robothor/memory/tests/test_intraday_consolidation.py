"""Tests for intra-day consolidation (Memory System v4.2 P0)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory.lifecycle import (
    _mark_facts_consolidated,
    find_consolidation_candidates,
    get_unconsolidated_count,
    run_intraday_consolidation,
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


class TestGetUnconsolidatedCount:
    def test_returns_count(self, mock_db):
        mock_db.fetchone.return_value = (7,)
        assert get_unconsolidated_count() == 7

    def test_returns_zero(self, mock_db):
        mock_db.fetchone.return_value = (0,)
        assert get_unconsolidated_count() == 0


class TestIntradaySkipsBelowThreshold:
    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self):
        with patch("robothor.memory.lifecycle.get_unconsolidated_count", return_value=3):
            result = await run_intraday_consolidation(threshold=5)
            assert result["skipped"] is True
            assert result["unconsolidated_count"] == 3
            assert result["threshold"] == 5

    @pytest.mark.asyncio
    async def test_skips_when_zero(self):
        with patch("robothor.memory.lifecycle.get_unconsolidated_count", return_value=0):
            result = await run_intraday_consolidation(threshold=5)
            assert result["skipped"] is True


class TestIntradayRunsAboveThreshold:
    @pytest.mark.asyncio
    async def test_runs_consolidation_and_marks(self):
        mock_group = [
            {"id": 1, "fact_text": "fact one", "category": "project", "entities": ["Robothor"]},
            {"id": 2, "fact_text": "fact two", "category": "project", "entities": ["Robothor"]},
        ]
        mock_result = {
            "consolidated_text": "combined fact",
            "source_ids": [1, 2],
        }

        with (
            patch("robothor.memory.lifecycle.get_unconsolidated_count", return_value=6),
            patch(
                "robothor.memory.lifecycle.find_consolidation_candidates",
                new_callable=AsyncMock,
                return_value=[mock_group],
            ),
            patch(
                "robothor.memory.lifecycle.consolidate_facts",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "robothor.memory.facts.store_fact",
                new_callable=AsyncMock,
                return_value=99,
            ),
            patch("robothor.memory.lifecycle.get_connection") as mock_conn,
            patch("robothor.memory.lifecycle._mark_facts_consolidated", return_value=4),
        ):
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            result = await run_intraday_consolidation(threshold=5)
            assert result["skipped"] is False
            assert result["consolidation_groups"] == 1
            assert result["facts_marked_consolidated"] == 4


class TestMarkFactsConsolidated:
    def test_marks_specific_ids(self, mock_db):
        mock_db.rowcount = 3
        result = _mark_facts_consolidated(fact_ids=[1, 2, 3])
        assert result == 3

    def test_marks_all_unconsolidated(self, mock_db):
        mock_db.rowcount = 10
        result = _mark_facts_consolidated()
        assert result == 10


class TestFindCandidatesUnconsolidatedOnly:
    @pytest.mark.asyncio
    async def test_unconsolidated_filter_in_sql(self):
        """Verify the SQL includes consolidated_at IS NULL filter."""
        with patch("robothor.memory.lifecycle.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            # Return empty to short-circuit
            cur.fetchall.return_value = []

            result = await find_consolidation_candidates(min_group_size=2, unconsolidated_only=True)
            assert result == []

            # Verify the SQL had the unconsolidated filter and lower LIMIT
            executed_sql = cur.execute.call_args[0][0]
            assert "consolidated_at IS NULL" in executed_sql
            assert "LIMIT 100" in executed_sql

    @pytest.mark.asyncio
    async def test_normal_mode_no_filter(self):
        """Verify the SQL does NOT include consolidated_at filter in normal mode."""
        with patch("robothor.memory.lifecycle.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            cur.fetchall.return_value = []

            await find_consolidation_candidates(min_group_size=3, unconsolidated_only=False)

            executed_sql = cur.execute.call_args[0][0]
            assert "consolidated_at IS NULL" not in executed_sql
            assert "LIMIT 500" in executed_sql
