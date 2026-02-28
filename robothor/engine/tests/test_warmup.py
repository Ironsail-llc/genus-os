"""Tests for the warmup module — session warmth preamble building."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.warmup import (
    MAX_WARMTH_CHARS,
    build_warmth_preamble,
)


@pytest.fixture
def empty_config() -> AgentConfig:
    return AgentConfig(id="test-agent", name="Test Agent")


@pytest.fixture
def warm_config() -> AgentConfig:
    return AgentConfig(
        id="email-responder",
        name="Email Responder",
        warmup_memory_blocks=["operational_findings"],
        warmup_context_files=["brain/memory/response-status.md"],
        warmup_peer_agents=["email-classifier"],
    )


# Patch targets — functions are imported lazily inside warmup.py
TRACKING_PATCH = "robothor.engine.tracking.get_schedule"
BLOCK_PATCH = "robothor.memory.blocks.read_block"


class TestBuildWarmthPreamble:
    """Tests for the main build_warmth_preamble function."""

    def test_empty_config_returns_empty(self, empty_config: AgentConfig, tmp_path: Path) -> None:
        with patch(TRACKING_PATCH, return_value=None):
            result = build_warmth_preamble(empty_config, tmp_path)
        assert result == ""

    def test_history_with_consecutive_errors(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        schedule = {
            "last_status": "failed",
            "last_duration_ms": 5000,
            "last_run_at": datetime.now(UTC) - timedelta(hours=2),
            "consecutive_errors": 3,
        }
        with patch(TRACKING_PATCH, return_value=schedule):
            result = build_warmth_preamble(config, tmp_path)
        assert "WARNING" in result
        assert "3 consecutive errors" in result

    def test_history_no_data_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        with patch(TRACKING_PATCH, return_value=None):
            result = build_warmth_preamble(config, tmp_path)
        assert result == ""

    def test_memory_block_injection(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["operational_findings"],
        )
        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value={"content": "Key finding: system is healthy."}),
        ):
            result = build_warmth_preamble(config, tmp_path)
        assert "MEMORY BLOCKS" in result
        assert "operational_findings" in result
        assert "Key finding" in result

    def test_memory_block_missing_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["nonexistent_block"],
        )
        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value={"content": ""}),
        ):
            result = build_warmth_preamble(config, tmp_path)
        assert result == ""

    def test_context_file_injection(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["status.md"],
        )
        status_file = tmp_path / "status.md"
        status_file.write_text("Last run: 2026-02-27 OK\nProcessed 5 emails.")

        with patch(TRACKING_PATCH, return_value=None):
            result = build_warmth_preamble(config, tmp_path)
        assert "CONTEXT FILES" in result
        assert "status.md" in result
        assert "Last run: 2026-02-27 OK" in result

    def test_context_file_missing_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["does-not-exist.md"],
        )
        with patch(TRACKING_PATCH, return_value=None):
            result = build_warmth_preamble(config, tmp_path)
        assert result == ""

    def test_peer_section(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_peer_agents=["email-classifier", "email-analyst"],
        )

        def side_effect(agent_id: str):
            if agent_id == "test-agent":
                return None
            if agent_id == "email-classifier":
                return {
                    "last_status": "completed",
                    "last_run_at": datetime.now(UTC) - timedelta(hours=1),
                    "consecutive_errors": 0,
                }
            if agent_id == "email-analyst":
                return {
                    "last_status": "failed",
                    "last_run_at": datetime.now(UTC) - timedelta(minutes=30),
                    "consecutive_errors": 2,
                }
            return None

        with patch(TRACKING_PATCH, side_effect=side_effect):
            result = build_warmth_preamble(config, tmp_path)
        assert "PEER AGENTS" in result
        assert "email-classifier: completed" in result
        assert "email-analyst: failed" in result
        assert "2 errors" in result

    def test_total_truncation(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["big_block"],
            warmup_context_files=["big_file.md"],
        )
        big_file = tmp_path / "big_file.md"
        big_file.write_text("y" * 5000)

        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value={"content": "x" * 5000}),
        ):
            result = build_warmth_preamble(config, tmp_path)
        assert len(result) <= MAX_WARMTH_CHARS + 50  # allow for truncation marker

    def test_history_section_completed_run(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        schedule = {
            "last_status": "completed",
            "last_duration_ms": 12345,
            "last_run_at": datetime.now(UTC) - timedelta(hours=3),
            "consecutive_errors": 0,
        }
        with patch(TRACKING_PATCH, return_value=schedule):
            result = build_warmth_preamble(config, tmp_path)
        assert "SESSION HISTORY" in result
        assert "completed" in result
        assert "12345ms" in result

    def test_all_sections_combined(self, tmp_path: Path) -> None:
        """Full warmup with all sections populated."""
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["findings"],
            warmup_context_files=["status.md"],
            warmup_peer_agents=["peer-1"],
        )
        status_file = tmp_path / "status.md"
        status_file.write_text("Agent OK")

        schedule_self = {
            "last_status": "completed",
            "last_duration_ms": 100,
            "last_run_at": datetime.now(UTC) - timedelta(hours=1),
            "consecutive_errors": 0,
        }
        schedule_peer = {
            "last_status": "completed",
            "last_run_at": datetime.now(UTC) - timedelta(hours=2),
            "consecutive_errors": 0,
        }

        def schedule_side_effect(agent_id: str):
            if agent_id == "test-agent":
                return schedule_self
            return schedule_peer

        with (
            patch(TRACKING_PATCH, side_effect=schedule_side_effect),
            patch(BLOCK_PATCH, return_value={"content": "block content here"}),
        ):
            result = build_warmth_preamble(config, tmp_path)

        assert "SESSION HISTORY" in result
        assert "MEMORY BLOCKS" in result
        assert "CONTEXT FILES" in result
        assert "PEER AGENTS" in result


class TestSchedulerWarmup:
    """Test that the scheduler properly injects warmup preamble."""

    def test_build_payload_includes_warmup(self, engine_config, sample_agent_config) -> None:
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        with patch(
            "robothor.engine.warmup.build_warmth_preamble",
            return_value="--- SESSION HISTORY ---\nLast run: completed",
        ):
            payload = scheduler._build_payload(sample_agent_config)
        assert "SESSION HISTORY" in payload
        assert "Execute your scheduled tasks" in payload

    def test_build_payload_empty_warmup(self, engine_config, sample_agent_config) -> None:
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        with patch("robothor.engine.warmup.build_warmth_preamble", return_value=""):
            payload = scheduler._build_payload(sample_agent_config)
        assert "SESSION HISTORY" not in payload
        assert "Execute your scheduled tasks" in payload
