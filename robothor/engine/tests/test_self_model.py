"""Tests for self-model generation (Phase 4: Dynamic Capability Self-Assessment)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.self_model import build_self_model, write_self_model


@pytest.fixture
def mock_analytics():
    """Mock all analytics functions."""
    fleet_health = {
        "agents": [
            {
                "agent_id": "main",
                "total_runs": 100,
                "completed": 95,
                "failed": 5,
                "success_rate": 0.95,
            },
            {
                "agent_id": "email-classifier",
                "total_runs": 50,
                "completed": 48,
                "failed": 2,
                "success_rate": 0.96,
            },
        ],
        "fleet_totals": {"total_runs": 150, "completed": 143, "failed": 7, "success_rate": 0.953},
    }
    agent_stats = {
        "total_runs": 100,
        "completed": 95,
        "failed": 5,
        "success_rate": 0.95,
        "avg_cost_usd": 0.12,
        "avg_duration_ms": 5000,
        "top_error_types": [{"error_type": "timeout", "count": 3}],
    }
    anomalies = {"anomalies": [], "agent_id": "main"}
    failure_patterns = {"patterns": []}

    with (
        patch("robothor.engine.self_model.get_fleet_health", return_value=fleet_health),
        patch("robothor.engine.self_model.get_agent_stats", return_value=agent_stats),
        patch("robothor.engine.self_model.detect_anomalies", return_value=anomalies),
        patch("robothor.engine.self_model.get_failure_patterns", return_value=failure_patterns),
    ):
        yield {
            "fleet_health": fleet_health,
            "agent_stats": agent_stats,
        }


@pytest.fixture
def mock_memory():
    """Mock memory stats and block reads."""
    memory_stats = {
        "total_facts": 500,
        "active_facts": 420,
        "entity_count": 85,
        "relation_count": 120,
    }

    with (
        patch("robothor.engine.self_model.get_memory_stats", return_value=memory_stats),
        patch(
            "robothor.engine.self_model.read_block", return_value={"content": "Prior learnings..."}
        ),
    ):
        yield memory_stats


@pytest.fixture
def mock_llm():
    """Mock LLM synthesis call."""
    model_output = """# System Self-Model — 2026-04-13

## Strengths
- main: 95% success rate, reliable

## Weaknesses
- calendar-monitor: frequent timeouts

## Coverage Gaps
- Entity graph: 85 entities, 120 relations

## Improvement Trajectory
- Fleet success rate: stable at 95%

## Recommended Priorities
1. Fix calendar-monitor timeouts
"""
    with patch(
        "robothor.engine.self_model.llm_client.generate",
        new_callable=AsyncMock,
        return_value=model_output,
    ):
        yield model_output


class TestBuildSelfModel:
    @pytest.mark.asyncio
    async def test_returns_structured_dict(self, mock_analytics, mock_memory, mock_llm):
        result = await build_self_model()
        assert "model_text" in result
        assert "generated_at" in result
        assert "fleet_health" in result
        assert "memory_stats" in result

    @pytest.mark.asyncio
    async def test_model_text_contains_sections(self, mock_analytics, mock_memory, mock_llm):
        result = await build_self_model()
        text = result["model_text"]
        assert "Strengths" in text
        assert "Weaknesses" in text

    @pytest.mark.asyncio
    async def test_includes_fleet_data(self, mock_analytics, mock_memory, mock_llm):
        result = await build_self_model()
        assert result["fleet_health"]["fleet_totals"]["total_runs"] == 150

    @pytest.mark.asyncio
    async def test_includes_memory_stats(self, mock_analytics, mock_memory, mock_llm):
        result = await build_self_model()
        assert result["memory_stats"]["total_facts"] == 500

    @pytest.mark.asyncio
    async def test_graceful_on_analytics_failure(self, mock_memory, mock_llm):
        """Should still produce a model even if some analytics fail."""
        with (
            patch("robothor.engine.self_model.get_fleet_health", side_effect=Exception("DB down")),
            patch("robothor.engine.self_model.get_agent_stats", return_value={}),
            patch("robothor.engine.self_model.detect_anomalies", return_value={"anomalies": []}),
            patch("robothor.engine.self_model.get_failure_patterns", return_value={"patterns": []}),
        ):
            result = await build_self_model()
            assert result is not None
            assert "model_text" in result


class TestWriteSelfModel:
    @pytest.mark.asyncio
    async def test_writes_to_self_model_block(self):
        model = {
            "model_text": "# Self Model\n\nTest content",
            "generated_at": "2026-04-13T04:00:00Z",
        }
        with patch("robothor.engine.self_model.write_block") as mock_write:
            await write_self_model(model)
            mock_write.assert_called_once()
            call_args = mock_write.call_args[0]
            assert call_args[0] == "self_model"
            assert "Self Model" in call_args[1]

    @pytest.mark.asyncio
    async def test_write_failure_does_not_raise(self):
        model = {"model_text": "test", "generated_at": "2026-04-13"}
        with patch("robothor.engine.self_model.write_block", side_effect=Exception("DB error")):
            # Should not raise
            await write_self_model(model)
