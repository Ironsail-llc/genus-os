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
        patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]),
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
  primary: anthropic/claude-sonnet-4.6
schedule:
  cron: ""
  timezone: America/New_York
  session_target: persistent
delivery:
  mode: none
  channel: telegram
  to: "99999999"
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
    to: "99999999"
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
            model_primary="anthropic/claude-sonnet-4.6",
            model_fallbacks=["openrouter/z-ai/glm-5"],
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
                delivery_to="99999999",
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
        assert override.delivery_to == "99999999"
        # Inherits model + tools from parent
        assert override.model_primary == "anthropic/claude-sonnet-4.6"
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

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            heartbeat=HeartbeatConfig(
                cron_expr="0 6-22/4 * * *",
                instruction_file="brain/HEARTBEAT.md",
            ),
        )

        acquire_calls = []

        def mock_acquire(key):
            acquire_calls.append(key)
            return False  # Simulate already running

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", side_effect=mock_acquire),
        ):
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


class TestStaleSchedulePruning:
    """Stale agent_schedules rows are pruned on startup."""

    @pytest.mark.asyncio
    async def test_stale_schedules_pruned(self, no_heartbeat_manifest):
        """Removed agents get their schedule rows deleted on startup."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=no_heartbeat_manifest,
            workspace=no_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        mock_delete = MagicMock(return_value=["supervisor"])
        with (
            patch("robothor.engine.scheduler.upsert_schedule"),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete),
            patch.object(scheduler.scheduler, "start"),
        ):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        # Should have been called with the set of active IDs
        mock_delete.assert_called_once()
        active_ids = mock_delete.call_args[0][0]
        assert "worker" in active_ids


class TestReconcileSchedules:
    """reconcile_schedules() prunes orphaned DB rows and APScheduler jobs."""

    def test_reconcile_prunes_stale_db_rows(self, no_heartbeat_manifest):
        """delete_stale_schedules is called with the correct active ID set."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=no_heartbeat_manifest,
            workspace=no_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        mock_delete = MagicMock(return_value=["supervisor"])
        with patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete):
            pruned = scheduler.reconcile_schedules()

        mock_delete.assert_called_once()
        active_ids = mock_delete.call_args[0][0]
        assert "worker" in active_ids
        assert "supervisor" in pruned

    def test_reconcile_removes_stale_apscheduler_jobs(self, no_heartbeat_manifest):
        """Orphaned APScheduler jobs are removed; legitimate jobs are kept."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=no_heartbeat_manifest,
            workspace=no_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        # Simulate APScheduler having a stale job and a legit one
        stale_job = MagicMock()
        stale_job.id = "supervisor"
        legit_job = MagicMock()
        legit_job.id = "worker"

        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_jobs.return_value = [stale_job, legit_job]

        with patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]):
            pruned = scheduler.reconcile_schedules()

        stale_job.remove.assert_called_once()
        legit_job.remove.assert_not_called()
        assert "supervisor" in pruned

    def test_reconcile_skips_workflow_jobs(self, no_heartbeat_manifest):
        """workflow:* prefixed jobs are never removed by reconciliation."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=no_heartbeat_manifest,
            workspace=no_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        wf_job = MagicMock()
        wf_job.id = "workflow:daily-report"

        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_jobs.return_value = [wf_job]

        with patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]):
            pruned = scheduler.reconcile_schedules()

        wf_job.remove.assert_not_called()
        assert "workflow:daily-report" not in pruned


class TestMisfireGraceTime:
    """Skip-if-stale is handled by APScheduler's misfire_grace_time."""

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_skip_if_stale_sets_grace_time(self, tmp_path):
        """Agents with catch_up=skip_if_stale should set misfire_grace_time."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "stale-agent.yaml").write_text(
            """id: stale-agent
name: Stale Agent
model:
  primary: openrouter/test/model
schedule:
  cron: "0 * * * *"
  timezone: UTC
  catch_up: skip_if_stale
  stale_after_minutes: 30
delivery:
  mode: none
tools_allowed: [exec]
instruction_file: ""
"""
        )

        config = EngineConfig(
            manifest_dir=manifest_dir,
            workspace=tmp_path,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        with patch.object(scheduler.scheduler, "start"):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        job = scheduler.scheduler.get_job("stale-agent")
        assert job is not None
        assert job.misfire_grace_time == 30 * 60  # stale_after_minutes * 60

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_coalesce_sets_no_grace_time(self, no_heartbeat_manifest):
        """Agents with catch_up=coalesce (default) should have grace_time=None."""
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

        job = scheduler.scheduler.get_job("worker")
        assert job is not None
        assert job.misfire_grace_time is None
