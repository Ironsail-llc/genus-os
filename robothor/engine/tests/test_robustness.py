"""Tests for Phase 2 robustness features — circuit breaker, DLQ, watchdog."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.dedup import clear as dedup_clear
from robothor.engine.models import AgentRun, RunStatus


@pytest.fixture(autouse=True)
def clean_dedup():
    dedup_clear()
    yield
    dedup_clear()


class TestCircuitBreaker:
    """Circuit breaker should skip agents with too many consecutive errors."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_after_threshold(self, engine_config, sample_agent_config):
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        schedule_data = {"consecutive_errors": 5}

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch("robothor.engine.tracking.get_schedule", return_value=schedule_data),
            patch("robothor.engine.delivery.get_telegram_sender", return_value=None),
            patch.object(runner, "execute", new_callable=AsyncMock) as mock_execute,
        ):
            await scheduler._run_agent("test-agent")
            mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_breaker_allows_below_threshold(self, engine_config, sample_agent_config):
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        schedule_data = {"consecutive_errors": 2}
        run = AgentRun(agent_id="test-agent", status=RunStatus.COMPLETED)
        run.started_at = None
        run.duration_ms = 100
        run.input_tokens = 10
        run.output_tokens = 5

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch("robothor.engine.tracking.get_schedule", return_value=schedule_data),
            patch("robothor.engine.tracking.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch("robothor.engine.delivery.deliver", new_callable=AsyncMock),
            patch.object(
                runner, "execute", new_callable=AsyncMock, return_value=run
            ) as mock_execute,
        ):
            await scheduler._run_agent("test-agent")
            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_circuit_breaker_creates_crm_task(self, engine_config, sample_agent_config):
        from unittest.mock import MagicMock

        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        sample_agent_config.delivery_to = "12345"
        schedule_data = {"consecutive_errors": 5}
        mock_create_task = MagicMock(return_value="task-123")

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch("robothor.engine.tracking.get_schedule", return_value=schedule_data),
            patch("robothor.crm.dal.create_task", mock_create_task),
            patch.object(runner, "execute", new_callable=AsyncMock) as mock_execute,
        ):
            await scheduler._run_agent("test-agent")
            mock_execute.assert_not_called()
            mock_create_task.assert_called_once()
            call_kwargs = mock_create_task.call_args
            assert "paused" in call_kwargs.kwargs.get("title", "")
            assert call_kwargs.kwargs.get("requires_human") is True
            assert call_kwargs.kwargs.get("priority") == "high"
            assert "needs-attention" in call_kwargs.kwargs.get("tags", [])


class TestCronDedup:
    """Scheduler dedup should prevent concurrent runs."""

    @pytest.mark.asyncio
    async def test_dedup_blocks_concurrent_run(self, engine_config, sample_agent_config):
        from robothor.engine.dedup import try_acquire
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        # Pre-acquire the lock
        try_acquire("test-agent")

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch.object(runner, "execute", new_callable=AsyncMock) as mock_execute,
        ):
            await scheduler._run_agent("test-agent")
            mock_execute.assert_not_called()


class TestExpandedTriggers:
    """Verify expanded EVENT_TRIGGERS."""

    def test_vision_trigger_registered(self):
        from robothor.engine.hooks import EVENT_TRIGGERS

        assert "vision" in EVENT_TRIGGERS
        triggers = EVENT_TRIGGERS["vision"]
        assert any(t["event_type"] == "vision.person_unknown" for t in triggers)
