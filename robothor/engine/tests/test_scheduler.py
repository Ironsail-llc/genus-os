"""Tests for the CronScheduler — heartbeat job registration and execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, HeartbeatConfig


@pytest.fixture
def _mock_tracking():
    """Mock tracking DB calls so scheduler doesn't hit Postgres."""
    with (
        patch("robothor.engine.scheduler.upsert_schedule"),
        patch("robothor.engine.scheduler.update_schedule_state"),
    ):
        yield


@pytest.fixture
def heartbeat_manifest(tmp_path):
    """Write a manifest with a heartbeat section and return its directory."""
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "main.yaml").write_text(
        """id: main
name: Robothor
model:
  primary: anthropic/claude-sonnet-4-6
schedule:
  cron: ""
  timezone: America/New_York
  session_target: persistent
delivery:
  mode: none
  channel: telegram
  to: "7636850023"
tools_allowed: [exec, read_file]
instruction_file: brain/SOUL.md
heartbeat:
  cron: "0 6-22/4 * * *"
  instruction_file: brain/HEARTBEAT.md
  session_target: isolated
  max_iterations: 15
  timeout_seconds: 600
  delivery:
    mode: announce
    channel: telegram
    to: "7636850023"
  context_files: [brain/memory/status.md]
  peer_agents: [email-classifier]
  bootstrap_files: [brain/AGENTS.md]
"""
    )
    return manifest_dir


@pytest.fixture
def no_heartbeat_manifest(tmp_path):
    """Write a manifest without heartbeat — plain cron agent."""
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "worker.yaml").write_text(
        """id: worker
name: Worker Agent
model:
  primary: openrouter/test/model
schedule:
  cron: "0 * * * *"
  timezone: UTC
delivery:
  mode: none
tools_allowed: [exec, read_file]
instruction_file: brain/WORKER.md
"""
    )
    return manifest_dir


class TestHeartbeatJobRegistration:
    """Heartbeat cron jobs are created when manifest has heartbeat section."""

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_heartbeat_job_created(self, heartbeat_manifest):
        """Heartbeat cron job is registered with ID {agent_id}:heartbeat."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=heartbeat_manifest,
            workspace=heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        # Patch the infinite loop so start() returns after loading
        with patch.object(scheduler.scheduler, "start"):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        job_ids = [j.id for j in scheduler.scheduler.get_jobs()]
        assert "main:heartbeat" in job_ids
        # main has no cron_expr so no regular cron job
        assert "main" not in job_ids

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_no_heartbeat_no_job(self, no_heartbeat_manifest):
        """Agents without heartbeat don't get heartbeat jobs."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=no_heartbeat_manifest,
            workspace=no_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        with patch.object(scheduler.scheduler, "start"):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        job_ids = [j.id for j in scheduler.scheduler.get_jobs()]
        assert "worker:heartbeat" not in job_ids
        # But regular cron job should exist
        assert "worker" in job_ids


class TestRunHeartbeat:
    """_run_heartbeat builds correct override config and calls runner."""

    @pytest.mark.asyncio
    async def test_run_heartbeat_builds_override(self, tmp_path):
        """_run_heartbeat creates an override AgentConfig from heartbeat settings."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        run_mock = MagicMock()
        run_mock.status.value = "completed"
        run_mock.started_at = None
        run_mock.id = "run-123"
        run_mock.duration_ms = 1000
        run_mock.input_tokens = 100
        run_mock.output_tokens = 50
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(
            manifest_dir=tmp_path,
            workspace=tmp_path,
        )
        scheduler = CronScheduler(config, runner)

        # Create a parent config with heartbeat
        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="anthropic/claude-sonnet-4-6",
            model_fallbacks=["openrouter/moonshotai/kimi-k2.5"],
            tools_allowed=["exec", "read_file", "list_tasks"],
            instruction_file="brain/SOUL.md",
            heartbeat=HeartbeatConfig(
                cron_expr="0 6-22/4 * * *",
                instruction_file="brain/HEARTBEAT.md",
                session_target="isolated",
                max_iterations=15,
                timeout_seconds=600,
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="7636850023",
                warmup_context_files=["brain/memory/status.md"],
                bootstrap_files=["brain/AGENTS.md"],
            ),
        )

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
        ):
            await scheduler._run_heartbeat("main")

        # Verify runner.execute was called
        runner.execute.assert_called_once()
        call_kwargs = runner.execute.call_args.kwargs

        # Check override config
        override = call_kwargs["agent_config"]
        assert override.instruction_file == "brain/HEARTBEAT.md"
        assert override.session_target == "isolated"
        assert override.max_iterations == 15
        assert override.delivery_mode == DeliveryMode.ANNOUNCE
        assert override.delivery_to == "7636850023"
        # Inherits model + tools from parent
        assert override.model_primary == "anthropic/claude-sonnet-4-6"
        assert override.tools_allowed == ["exec", "read_file", "list_tasks"]
        # token_budget is auto-derived at runtime, not from heartbeat config
        assert override.token_budget == 0

    @pytest.mark.asyncio
    async def test_heartbeat_dedup_key_isolation(self, tmp_path):
        """Heartbeat uses {agent_id}:heartbeat dedup key, not the agent ID."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = MagicMock()
        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        acquire_calls = []

        def mock_acquire(key):
            acquire_calls.append(key)
            return False  # Simulate already running

        with patch("robothor.engine.scheduler.try_acquire", side_effect=mock_acquire):
            await scheduler._run_heartbeat("main")

        assert acquire_calls == ["main:heartbeat"]

    @pytest.mark.asyncio
    async def test_heartbeat_skipped_when_no_config(self, tmp_path):
        """_run_heartbeat returns gracefully if agent config not found."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        with (
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.config.load_agent_config", return_value=None),
        ):
            await scheduler._run_heartbeat("main")

        runner.execute.assert_not_called()
