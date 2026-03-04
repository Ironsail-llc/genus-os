"""Tests for observability quick wins — delivery_status, tool events, health check."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.delivery import deliver, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus

# ─── Helpers ────────────────────────────────────────────────────────


def _make_run(**kwargs) -> AgentRun:
    defaults = {
        "id": "run-1",
        "agent_id": "test",
        "status": RunStatus.COMPLETED,
        "output_text": "Hello",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)


def _make_config(**kwargs) -> AgentConfig:
    defaults = {
        "id": "test",
        "name": "Test",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "12345",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)


# ─── delivery_status Tests ──────────────────────────────────────────


class TestDeliveryStatus:
    @pytest.fixture(autouse=True)
    def _setup_sender(self):
        sender = AsyncMock()
        set_telegram_sender(sender)
        yield sender
        set_telegram_sender(None)

    @pytest.mark.asyncio
    async def test_sub_agent_suppressed(self):
        """Sub-agent runs get delivery_status='suppressed_sub_agent'."""
        config = _make_config()
        run = _make_run(parent_run_id="parent-123")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "suppressed_sub_agent"

    @pytest.mark.asyncio
    async def test_no_output_status(self):
        """Runs with no output get delivery_status='no_output'."""
        config = _make_config()
        run = _make_run(output_text=None)
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"

    @pytest.mark.asyncio
    async def test_empty_output_status(self):
        """Runs with empty output get delivery_status='no_output'."""
        config = _make_config()
        run = _make_run(output_text="")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"

    @pytest.mark.asyncio
    async def test_trailing_heartbeat_ok_stripped(self, _setup_sender):
        """Trailing HEARTBEAT_OK is stripped but report still delivered."""
        config = _make_config()
        run = _make_run(output_text="All systems nominal.\n\nHEARTBEAT_OK")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "delivered"
        # Verify the cleaned text was sent (without HEARTBEAT_OK)
        sent_text = _setup_sender.call_args[0][1]
        assert "HEARTBEAT_OK" not in sent_text
        assert "All systems nominal." in sent_text

    @pytest.mark.asyncio
    async def test_bare_heartbeat_ok_treated_as_no_output(self):
        """Bare HEARTBEAT_OK (alone) results in no_output."""
        config = _make_config()
        run = _make_run(output_text="  HEARTBEAT_OK  \n")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"

    @pytest.mark.asyncio
    async def test_none_mode_silent(self):
        """delivery_mode=none gets delivery_status='silent'."""
        config = _make_config(delivery_mode=DeliveryMode.NONE)
        run = _make_run()
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "silent"

    @pytest.mark.asyncio
    async def test_announce_delivered(self, _setup_sender):
        """Successful Telegram delivery sets delivery_status='delivered'."""
        config = _make_config()
        run = _make_run()
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "delivered"
        assert run.delivery_channel == "telegram"
        assert run.delivered_at is not None

    @pytest.mark.asyncio
    async def test_announce_failed(self, _setup_sender):
        """Failed Telegram delivery sets delivery_status starting with 'failed'."""
        _setup_sender.side_effect = RuntimeError("Network error")
        config = _make_config()
        run = _make_run()
        result = await deliver(config, run)
        assert result is False
        assert run.delivery_status.startswith("failed")


# ─── Tool Event Logging Tests ──────────────────────────────────────


class TestLogToolEvent:
    def test_logs_successful_event(self, mock_db):
        """log_tool_event inserts a row for successful tool calls."""
        from robothor.engine.tracking import log_tool_event

        log_tool_event(
            run_id="run-1",
            tool_name="list_tasks",
            duration_ms=150,
            success=True,
        )
        mock_db["cursor"].execute.assert_called_once()
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "agent_tool_events" in sql

    def test_logs_failed_event_with_error_type(self, mock_db):
        """log_tool_event records error_type for failed calls."""
        from robothor.engine.tracking import log_tool_event

        log_tool_event(
            run_id="run-1",
            tool_name="exec",
            duration_ms=5000,
            success=False,
            error_type="timeout",
        )
        args = mock_db["cursor"].execute.call_args[0][1]
        assert args[4] is False  # success
        assert args[5] == "timeout"  # error_type

    def test_db_failure_silently_caught(self):
        """log_tool_event doesn't raise on DB errors."""
        from robothor.engine.tracking import log_tool_event

        with patch("robothor.engine.tracking.get_connection", side_effect=Exception("DB down")):
            # Should not raise
            log_tool_event(
                run_id="run-1",
                tool_name="read_file",
                duration_ms=10,
                success=True,
            )


# ─── Tool Stats Tests ──────────────────────────────────────────────


class TestGetToolStats:
    def test_returns_aggregated_stats(self, mock_db):
        """get_tool_stats returns per-tool aggregated data."""
        from robothor.engine.tracking import get_tool_stats

        mock_db["cursor"].fetchall.return_value = [
            {
                "tool_name": "exec",
                "total_calls": 50,
                "successes": 48,
                "failures": 2,
                "avg_duration_ms": 3000,
                "max_duration_ms": 15000,
                "p95_duration_ms": 10000,
            }
        ]
        results = get_tool_stats(hours=24)
        assert len(results) == 1
        assert results[0]["tool_name"] == "exec"
        assert results[0]["failures"] == 2


# ─── Cron Health Check Tests ───────────────────────────────────────


class TestCronHealthCheck:
    def test_classify_agent_healthy(self):
        """Agent with successes and low failure rate is healthy."""
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import classify_agent

        agent = {
            "total_runs": 10,
            "completed": 9,
            "failed": 1,
            "timeouts": 0,
            "last_success_at": "2026-03-04",
        }
        assert classify_agent(agent) == "healthy"

    def test_classify_agent_error_high_fail_rate(self):
        """Agent with >50% failure rate is error."""
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import classify_agent

        agent = {
            "total_runs": 4,
            "completed": 1,
            "failed": 3,
            "timeouts": 0,
            "last_success_at": "2026-03-04",
        }
        assert classify_agent(agent) == "error"

    def test_classify_agent_stale_no_runs(self):
        """Agent with 0 runs is stale."""
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import classify_agent

        agent = {
            "total_runs": 0,
            "completed": 0,
            "failed": 0,
            "timeouts": 0,
            "last_success_at": None,
        }
        assert classify_agent(agent) == "stale"

    def test_classify_agent_no_success_but_no_failures(self):
        """Agent with runs but no successes AND no failures is healthy (still running)."""
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import classify_agent

        agent = {
            "total_runs": 2,
            "completed": 0,
            "failed": 0,
            "timeouts": 0,
            "last_success_at": None,
        }
        assert classify_agent(agent) == "healthy"

    def test_format_duration(self):
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import format_duration

        assert format_duration(None) == "—"
        assert format_duration(0) == "—"
        assert format_duration(500) == "500ms"
        assert format_duration(5000) == "5s"

    def test_format_cost(self):
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import format_cost

        assert format_cost(None) == "$0"
        assert format_cost(0) == "$0"
        assert format_cost(0.005) == "$0.0050"
        assert format_cost(1.23) == "$1.23"

    def test_write_status_creates_file(self, tmp_path):
        """write_status creates a markdown file."""
        import sys

        sys.path.insert(0, "/home/philip/robothor/brain/scripts")
        from cron_health_check import write_status

        output = tmp_path / "status.md"
        agents = [
            {
                "agent_id": "test-agent",
                "total_runs": 10,
                "completed": 9,
                "failed": 1,
                "timeouts": 0,
                "avg_duration_ms": 150,
                "total_cost_usd": 0.05,
                "last_run_at": None,
                "last_success_at": "2026-03-04",
            }
        ]
        fleet = {
            "total_runs": 10,
            "completed": 9,
            "failed": 1,
            "timeouts": 0,
            "total_cost_usd": 0.05,
            "avg_duration_ms": 150,
        }
        tools = {"slowest": [], "failing": []}
        write_status(agents, fleet, tools, output_path=output)
        content = output.read_text()
        assert "# Cron Health Status" in content
        assert "test-agent" in content
        assert "Fleet Summary" in content
