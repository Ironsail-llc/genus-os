"""Tests for the CronScheduler — heartbeat job registration and execution."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, HeartbeatConfig, WorkerConfig


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
def reporter_heartbeat_manifest(tmp_path):
    """Manifest mirroring delphi-main: parent delivery=none, heartbeat=announce.

    This pattern lets a normally-silent reporter agent emit a periodic
    digest without changing how interactive sessions deliver. Verifies
    the override path correctly flips delivery_mode + uses
    task_authorship_agent for CRM attribution.
    """
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "reporter.yaml").write_text(
        """id: reporter
name: Reporter
model:
  primary: anthropic/claude-sonnet-4.6
schedule:
  cron: ""
  timezone: America/New_York
  session_target: persistent
delivery:
  mode: none
  channel: telegram
  to: "${REPORTER_CHAT_ID}"
tools_allowed: [exec, search_memory, memory_block_read, list_tasks, create_task]
instruction_file: brain/REPORTER_MAIN.md
heartbeat:
  cron: "0 9,14,20 * * *"
  timezone: America/New_York
  instruction_file: brain/REPORTER_HEARTBEAT.md
  session_target: persistent
  max_iterations: 12
  cost_budget_usd: 0.40
  persistent_history_limit: 1
  delivery:
    mode: announce
    channel: telegram
    to: "${REPORTER_CHAT_ID}"
  memory_blocks: [reporter-state, reporter-perf-log]
  heartbeat_tools_allowed:
    [exec, search_memory, memory_block_read, list_tasks, list_my_tasks,
     create_task, update_task, send_notification, todo_write]
  task_authorship_agent: reporter-scout
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
    async def test_reporter_heartbeat_job_created(self, reporter_heartbeat_manifest):
        """Reporter manifest (mode: none parent + heartbeat) registers heartbeat job.

        Mirrors the delphi-main pattern: parent agent is interactive-only
        (mode: none, no cron), heartbeat block adds a scheduled scout beat.
        """
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(
            manifest_dir=reporter_heartbeat_manifest,
            workspace=reporter_heartbeat_manifest.parent.parent,
        )
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        with patch.object(scheduler.scheduler, "start"):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        job_ids = [j.id for j in scheduler.scheduler.get_jobs()]
        assert "reporter:heartbeat" in job_ids
        # Parent has no cron_expr, so no regular cron job
        assert "reporter" not in job_ids

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
            model_fallbacks=["openrouter/xiaomi/mimo-v2-pro"],
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

    def test_heartbeat_flips_silent_parent_to_announce(self):
        """Reporter pattern: parent delivery=NONE, heartbeat delivery=ANNOUNCE.

        delphi-main is normally silent (interactive-only, mode=none). The
        scout heartbeat needs to actively announce to Telegram. Verifies
        the override sets delivery_mode + task_author_override correctly.
        """
        from robothor.engine.scheduler import _build_heartbeat_config

        parent = AgentConfig(
            id="reporter",
            name="Reporter",
            model_primary="anthropic/claude-sonnet-4.6",
            tools_allowed=["exec", "search_memory"],
            instruction_file="brain/REPORTER_MAIN.md",
            delivery_mode=DeliveryMode.NONE,
            delivery_channel="telegram",
            delivery_to="${REPORTER_CHAT_ID}",
            heartbeat=HeartbeatConfig(
                cron_expr="0 9,14,20 * * *",
                instruction_file="brain/REPORTER_HEARTBEAT.md",
                session_target="persistent",
                max_iterations=12,
                cost_budget_usd=0.40,
                persistent_history_limit=1,
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="${REPORTER_CHAT_ID}",
                tools_allowed=["exec", "search_memory", "create_task"],
                task_authorship_agent="reporter-scout",
            ),
        )

        override = _build_heartbeat_config(parent)

        assert override.delivery_mode == DeliveryMode.ANNOUNCE
        assert override.delivery_to == "${REPORTER_CHAT_ID}"
        assert override.instruction_file == "brain/REPORTER_HEARTBEAT.md"
        assert override.tools_allowed == ["exec", "search_memory", "create_task"]
        # Hard budget kicks in when heartbeat sets its own cost cap
        assert override.max_cost_usd == 0.40
        assert override.hard_budget is True
        # Scout filings attributed to scout identity, not parent
        assert override.task_author_override == "reporter-scout"
        # Model still inherits from parent
        assert override.model_primary == "anthropic/claude-sonnet-4.6"

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

    def test_reconcile_keeps_worker_jobs(self, tmp_path):
        """Worker jobs (`{agent_id}:worker`) are part of active_ids and not pruned.

        Regression test: prior to this fix, reconcile_schedules() forgot to
        include worker IDs, so the watchdog pruned `main:worker` 5 minutes
        after every engine start, breaking the drain cycle.
        """
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "main.yaml").write_text(
            """id: main
name: Main
model:
  primary: anthropic/claude-sonnet-4.6
schedule:
  cron: ""
  timezone: America/New_York
delivery:
  mode: none
tools_allowed: [read_file]
instruction_file: brain/SOUL.md
heartbeat:
  cron: "0 * * * *"
  instruction_file: brain/HEARTBEAT.md
worker:
  cron: "0 7-22/2 * * *"
  instruction_file: brain/WORKER.md
  delivery:
    mode: announce
    channel: telegram
    to: "99999999"
"""
        )

        config = EngineConfig(manifest_dir=manifest_dir, workspace=tmp_path)
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        worker_job = MagicMock()
        worker_job.id = "main:worker"
        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_jobs.return_value = [worker_job]

        mock_delete = MagicMock(return_value=[])
        with patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete):
            scheduler.reconcile_schedules()

        active_ids = mock_delete.call_args[0][0]
        assert "main:worker" in active_ids, "worker IDs must be in active_ids set"
        worker_job.remove.assert_not_called()

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


class TestCronCatchUp:
    """Startup catch-up: fire a cron occurrence missed while the daemon was down.

    APScheduler's in-memory jobstore has no memory of occurrences missed
    between process restarts — misfire_grace_time only covers misses within
    a single process's uptime. On start(), the scheduler compares each cron
    agent's persisted last_run_at against the most recent scheduled
    occurrence and spawns a catch-up run when one was missed.
    """

    def _write_manifest(self, tmp_path, *, catch_up="coalesce", stale_after_minutes=120):
        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "catchup-agent.yaml").write_text(
            f"""id: catchup-agent
name: Catchup Agent
model:
  primary: openrouter/test/model
schedule:
  cron: "0 * * * *"
  timezone: UTC
  catch_up: {catch_up}
  stale_after_minutes: {stale_after_minutes}
delivery:
  mode: none
tools_allowed: [exec]
instruction_file: ""
"""
        )
        return manifest_dir

    async def _run_start_and_capture_spawns(self, tmp_path, *, manifest_dir, schedule_row, now):
        """Drive scheduler.start() up to the poll loop; return catch-up spawn names."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=manifest_dir, workspace=tmp_path)
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        spawned: list[str] = []

        def _capture_spawn(coro, *, name=None):
            spawned.append(name)
            coro.close()  # never actually run it — avoid an unawaited-coroutine warning
            return MagicMock()

        mock_registry = MagicMock()
        mock_registry.spawn.side_effect = _capture_spawn

        with (
            patch("robothor.engine.scheduler.get_schedule", return_value=schedule_row),
            patch("robothor.engine.scheduler.get_task_registry", return_value=mock_registry),
            patch("robothor.engine.scheduler._catchup_now", return_value=now),
            patch.object(scheduler.scheduler, "start"),
        ):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        return spawned

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_missed_occurrence_within_window_spawns_once(self, tmp_path):
        """coalesce (default): a miss is caught up regardless of age."""
        manifest_dir = self._write_manifest(tmp_path, catch_up="coalesce")
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)  # prev occurrence: 10:00
        schedule_row = {"last_run_at": datetime(2026, 8, 17, 8, 0, tzinfo=UTC)}  # missed 10:00

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=schedule_row, now=now
        )

        assert spawned.count("cron-catchup:catchup-agent") == 1

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_skip_if_stale_beyond_window_no_spawn(self, tmp_path):
        """skip_if_stale: a miss older than stale_after_minutes is dropped."""
        manifest_dir = self._write_manifest(
            tmp_path, catch_up="skip_if_stale", stale_after_minutes=5
        )
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)  # prev occurrence: 10:00, 15m ago
        schedule_row = {"last_run_at": datetime(2026, 8, 17, 8, 0, tzinfo=UTC)}

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=schedule_row, now=now
        )

        assert spawned == []

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_skip_if_stale_within_window_spawns_once(self, tmp_path):
        """skip_if_stale: a miss inside stale_after_minutes is still caught up."""
        manifest_dir = self._write_manifest(
            tmp_path, catch_up="skip_if_stale", stale_after_minutes=30
        )
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)  # prev occurrence: 10:00, 15m ago
        schedule_row = {"last_run_at": datetime(2026, 8, 17, 8, 0, tzinfo=UTC)}

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=schedule_row, now=now
        )

        assert spawned.count("cron-catchup:catchup-agent") == 1

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_no_miss_no_spawn(self, tmp_path):
        """last_run_at already covers the most recent occurrence — nothing missed."""
        manifest_dir = self._write_manifest(tmp_path, catch_up="coalesce")
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)  # prev occurrence: 10:00
        schedule_row = {"last_run_at": datetime(2026, 8, 17, 10, 5, tzinfo=UTC)}  # after 10:00

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=schedule_row, now=now
        )

        assert spawned == []

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_no_baseline_no_spawn(self, tmp_path):
        """last_run_at is None (never run) — no baseline, avoid a startup stampede."""
        manifest_dir = self._write_manifest(tmp_path, catch_up="coalesce")
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)
        schedule_row = {"last_run_at": None}

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=schedule_row, now=now
        )

        assert spawned == []

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_missing_schedule_row_no_spawn(self, tmp_path):
        """No schedule row at all for this agent — nothing to catch up against."""
        manifest_dir = self._write_manifest(tmp_path, catch_up="coalesce")
        now = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)

        spawned = await self._run_start_and_capture_spawns(
            tmp_path, manifest_dir=manifest_dir, schedule_row=None, now=now
        )

        assert spawned == []


class TestPersistentSessionSaveGate:
    """save_exchange must refuse to persist degenerate heartbeat outputs.

    The persistent session for `cron:main:heartbeat` poisons future beats
    when mid-thought fragments or budget-capped runs get saved. The gate
    in _execute_and_deliver filters these out before persistence.
    """

    def _mk_parent_config(self):
        return AgentConfig(
            id="main",
            name="Robothor",
            model_primary="anthropic/claude-sonnet-4.6",
            tools_allowed=["read_file", "list_tasks"],
            instruction_file="brain/SOUL.md",
            session_target="persistent",
            persistent_history_limit=6,
            heartbeat=HeartbeatConfig(
                cron_expr="0 6-22 * * *",
                instruction_file="brain/HEARTBEAT.md",
                session_target="persistent",
                persistent_history_limit=6,
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="99999999",
            ),
        )

    async def _run_beat_and_capture_save(self, tmp_path, *, mutate_run):
        """Drive one heartbeat execution, return whether save_exchange was called."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.models import RunStatus
        from robothor.engine.scheduler import CronScheduler

        parent_config = self._mk_parent_config()

        run_mock = MagicMock()
        run_mock.status = RunStatus.COMPLETED
        run_mock.output_text = "**⚡ Beat report** — all quiet.\n- 0 new tasks"
        run_mock.id = "run-xyz"
        run_mock.error_message = None
        run_mock.budget_exhausted = False
        run_mock.steps = []
        run_mock.parent_run_id = None
        run_mock.delivery_status = None
        run_mock.delivered_at = None
        mutate_run(run_mock)

        runner = AsyncMock()
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        save_calls: list[tuple] = []

        def fake_save_exchange(session_key, user_msg, assistant_msg, channel="cron"):
            save_calls.append((session_key, user_msg, assistant_msg, channel))

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.scheduler.upsert_schedule"),
            patch("robothor.engine.tracking.get_schedule", return_value=None),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch("robothor.engine.chat_store.load_session", return_value=None),
            patch("robothor.engine.chat_store.save_exchange", side_effect=fake_save_exchange),
        ):
            await scheduler._run_heartbeat("main")

        return save_calls

    @pytest.mark.asyncio
    async def test_clean_beat_is_saved(self, tmp_path):
        """Sanity: a normal, completed, clean heartbeat IS persisted."""
        save_calls = await self._run_beat_and_capture_save(tmp_path, mutate_run=lambda r: None)
        assert len(save_calls) == 1, f"clean beat should save, got {save_calls}"

    @pytest.mark.asyncio
    async def test_timeout_beat_is_not_saved(self, tmp_path):
        """Timeouts must not poison the persistent session."""
        from robothor.engine.models import RunStatus

        def mutate(r):
            r.status = RunStatus.TIMEOUT
            r.error_message = "Agent execution timed out"
            r.output_text = ""

        save_calls = await self._run_beat_and_capture_save(tmp_path, mutate_run=mutate)
        assert save_calls == [], f"timeout should not save, got {save_calls}"

    @pytest.mark.asyncio
    async def test_budget_exhausted_beat_is_not_saved(self, tmp_path):
        """Hitting the cost cap means the run was cut off mid-stream — never persist."""

        def mutate(r):
            r.budget_exhausted = True
            r.output_text = "Now let me continue with the next step:"

        save_calls = await self._run_beat_and_capture_save(tmp_path, mutate_run=mutate)
        assert save_calls == [], f"budget-exhausted should not save, got {save_calls}"

    @pytest.mark.asyncio
    async def test_midthought_beat_is_not_saved(self, tmp_path):
        """A completed run with mid-thought output must not persist."""

        def mutate(r):
            r.output_text = "Good — I can see the thread. Now let me send the reply:"

        save_calls = await self._run_beat_and_capture_save(tmp_path, mutate_run=mutate)
        assert save_calls == [], f"mid-thought should not save, got {save_calls}"

    @pytest.mark.asyncio
    async def test_short_fragment_beat_is_not_saved(self, tmp_path):
        """Short single-line output (no structure) is suspect and not persisted."""

        def mutate(r):
            r.output_text = (
                "All 3 deleted. Now reply to the thread confirming, and archive the emails:"
            )

        save_calls = await self._run_beat_and_capture_save(tmp_path, mutate_run=mutate)
        assert save_calls == [], f"short fragment should not save, got {save_calls}"


class TestHeartbeatStatusPing:
    """Heartbeat always surfaces SOMETHING to Telegram — never silent failure.

    Operator's primary complaint: 'I haven't received any heartbeats today.'
    When delivery is silent (timeout with no output, suppressed trivial,
    failed telegram call), emit a one-line fallback ping so the operator
    knows the engine is alive.
    """

    def _mk_parent_config(self):
        return AgentConfig(
            id="main",
            name="Robothor",
            model_primary="anthropic/claude-sonnet-4.6",
            tools_allowed=["read_file"],
            instruction_file="brain/SOUL.md",
            session_target="persistent",
            heartbeat=HeartbeatConfig(
                cron_expr="0 6-22 * * *",
                instruction_file="brain/HEARTBEAT.md",
                session_target="persistent",
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="99999999",
            ),
        )

    async def _drive_heartbeat(self, tmp_path, *, delivery_status, run_status=None):
        """Drive one heartbeat, return captured telegram sends."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.models import RunStatus
        from robothor.engine.scheduler import CronScheduler

        parent_config = self._mk_parent_config()

        run_mock = MagicMock()
        run_mock.status = run_status or RunStatus.COMPLETED
        run_mock.output_text = ""
        run_mock.id = "run-abc"
        run_mock.error_message = None
        run_mock.budget_exhausted = False
        run_mock.steps = []
        run_mock.parent_run_id = None
        run_mock.delivery_status = delivery_status
        run_mock.delivered_at = None
        run_mock.duration_ms = 1000
        run_mock.input_tokens = 0
        run_mock.output_tokens = 0
        run_mock.total_cost_usd = 0.0

        runner = AsyncMock()
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        sends: list[tuple] = []

        async def fake_sender(chat_id, text):
            sends.append((chat_id, text))
            return []

        from robothor.engine.delivery import set_telegram_sender

        set_telegram_sender(fake_sender)
        try:
            with (
                patch("robothor.engine.config.load_agent_config", return_value=parent_config),
                patch("robothor.engine.scheduler.try_acquire", return_value=True),
                patch("robothor.engine.scheduler.release"),
                patch(
                    "robothor.engine.scheduler.deliver",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch("robothor.engine.scheduler.update_schedule_state"),
                patch("robothor.engine.scheduler.upsert_schedule"),
                patch("robothor.engine.tracking.get_schedule", return_value=None),
                patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
                patch("robothor.engine.chat_store.load_session", return_value=None),
                patch("robothor.engine.chat_store.save_exchange"),
            ):
                await scheduler._run_heartbeat("main")
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

        return sends

    @pytest.mark.asyncio
    async def test_no_output_emits_status_ping(self, tmp_path):
        """delivery_status='no_output' means operator saw nothing → ping."""
        sends = await self._drive_heartbeat(tmp_path, delivery_status="no_output")
        assert len(sends) == 1, f"expected status ping, got {sends}"
        assert "heartbeat" in sends[0][1].lower()

    @pytest.mark.asyncio
    async def test_timeout_emits_status_ping(self, tmp_path):
        """Run-level timeout with no output → ping."""
        from robothor.engine.models import RunStatus

        sends = await self._drive_heartbeat(
            tmp_path, delivery_status=None, run_status=RunStatus.TIMEOUT
        )
        assert len(sends) == 1, f"expected status ping, got {sends}"

    @pytest.mark.asyncio
    async def test_suppressed_trivial_emits_status_ping(self, tmp_path):
        """Even suppressed-trivial should ping — operator wants to know beat ran."""
        sends = await self._drive_heartbeat(tmp_path, delivery_status="suppressed_trivial")
        assert len(sends) == 1

    @pytest.mark.asyncio
    async def test_delivered_does_not_emit_status_ping(self, tmp_path):
        """Normal 'delivered' beats must NOT double-send."""
        sends = await self._drive_heartbeat(tmp_path, delivery_status="delivered")
        assert sends == [], f"should not ping on success, got {sends}"

    @pytest.mark.asyncio
    async def test_failed_delivery_emits_status_ping(self, tmp_path):
        """When _deliver_telegram fails, delivery_status='failed: …' → ping."""
        sends = await self._drive_heartbeat(tmp_path, delivery_status="failed: telegram timeout")
        assert len(sends) == 1


class TestHeartbeatHistoryFilter:
    """Poisoned prior messages must be dropped BEFORE they reach the runner.

    The save-gate prevents NEW bad outputs from being persisted, but a
    session may already contain fragments from before the gate landed. The
    load-side filter drops those so the next beat never sees them.
    """

    async def _drive_with_history(self, tmp_path, raw_history):
        """Drive one heartbeat and capture the conversation_history passed to runner."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.models import RunStatus
        from robothor.engine.scheduler import CronScheduler

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="anthropic/claude-sonnet-4.6",
            tools_allowed=["read_file"],
            instruction_file="brain/SOUL.md",
            session_target="persistent",
            persistent_history_limit=6,
            heartbeat=HeartbeatConfig(
                cron_expr="0 6-22 * * *",
                instruction_file="brain/HEARTBEAT.md",
                session_target="persistent",
                persistent_history_limit=6,
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="99999999",
            ),
        )

        run_mock = MagicMock()
        run_mock.status = RunStatus.COMPLETED
        run_mock.output_text = "**Beat report** — all clear.\n- 0 tasks"
        run_mock.id = "run-x"
        run_mock.error_message = None
        run_mock.budget_exhausted = False
        run_mock.steps = []
        run_mock.parent_run_id = None
        run_mock.delivery_status = "delivered"
        run_mock.delivered_at = None

        runner = AsyncMock()
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock, return_value=True),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.scheduler.upsert_schedule"),
            patch("robothor.engine.tracking.get_schedule", return_value=None),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch(
                "robothor.engine.chat_store.load_session",
                return_value={"history": raw_history, "model_override": None},
            ),
            patch("robothor.engine.chat_store.save_exchange"),
        ):
            await scheduler._run_heartbeat("main")

        call_kwargs = runner.execute.call_args.kwargs
        return call_kwargs.get("conversation_history")

    @pytest.mark.asyncio
    async def test_clean_history_passes_through(self, tmp_path):
        """Clean prior beats are kept."""
        raw = [
            {"role": "user", "content": "Current time: 12:00 UTC\nExecute tasks."},
            {
                "role": "assistant",
                "content": "**Beat report**\n- 0 tasks\n- Fleet green.",
            },
        ]
        passed = await self._drive_with_history(tmp_path, raw)
        assert passed == raw

    @pytest.mark.asyncio
    async def test_midthought_assistant_turn_dropped(self, tmp_path):
        """An assistant turn matching _looks_like_mid_thought is removed."""
        raw = [
            {"role": "user", "content": "tick"},
            {
                "role": "assistant",
                "content": "The verification flags are expected — same as before.",
            },
            {"role": "user", "content": "tick"},
            {
                "role": "assistant",
                "content": "**Beat** — all clear.\n- 0 new tasks.",
            },
        ]
        passed = await self._drive_with_history(tmp_path, raw)
        contents = [m.get("content", "") for m in passed]
        assert not any("verification flags" in c for c in contents)
        assert any("**Beat**" in c for c in contents)

    @pytest.mark.asyncio
    async def test_short_fragment_assistant_dropped(self, tmp_path):
        """A short one-line fragment ending in colon is dropped."""
        raw = [
            {"role": "user", "content": "tick"},
            {
                "role": "assistant",
                "content": "All 3 deleted. Now reply to the thread:",
            },
        ]
        passed = await self._drive_with_history(tmp_path, raw)
        contents = [m.get("content", "") for m in passed]
        assert not any("All 3 deleted" in c for c in contents)

    @pytest.mark.asyncio
    async def test_user_turns_never_dropped(self, tmp_path):
        """Even if a user message looks weird, user turns are never filtered."""
        raw = [
            {
                "role": "user",
                "content": "The verification flags are expected:",
            },
            {
                "role": "assistant",
                "content": "**Report**\n- acknowledged.",
            },
        ]
        passed = await self._drive_with_history(tmp_path, raw)
        contents = [m.get("content", "") for m in passed]
        # User turn preserved (otherwise we'd desynchronize the exchange pairing).
        assert any("verification flags" in c for c in contents)


class TestDrainWorkerMode:
    """Drain / worker mode: symmetric to heartbeat but executes the queue.

    One main identity, two cron modes: `main:heartbeat` (scout, scan-and-file)
    and `main:worker` (drain, execute). This suite verifies the scheduler
    registers, invokes, and budgets the drain cycle correctly.
    """

    @pytest.mark.usefixtures("_mock_tracking")
    @pytest.mark.asyncio
    async def test_worker_job_registered(self, tmp_path):
        """When agent_config.worker.cron_expr is set, scheduler registers
        a `{agent_id}:worker` job alongside any heartbeat/regular jobs."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

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
delivery:
  mode: none
tools_allowed: [read_file]
instruction_file: brain/SOUL.md
heartbeat:
  cron: "0 * * * *"
  instruction_file: brain/HEARTBEAT.md
worker:
  cron: "0 7-22/2 * * *"
  instruction_file: brain/WORKER.md
  delivery:
    mode: announce
    channel: telegram
    to: "99999999"
"""
        )

        config = EngineConfig(manifest_dir=manifest_dir, workspace=tmp_path)
        runner = MagicMock()
        scheduler = CronScheduler(config, runner)

        with patch.object(scheduler.scheduler, "start"):
            import asyncio

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.start()

        job_ids = [j.id for j in scheduler.scheduler.get_jobs()]
        assert "main:heartbeat" in job_ids
        assert "main:worker" in job_ids
        # No top-level cron, so no plain "main" job
        assert "main" not in job_ids

    @pytest.mark.asyncio
    async def test_run_worker_builds_override(self, tmp_path):
        """_run_worker creates an override AgentConfig from worker settings."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        run_mock = MagicMock()
        run_mock.status.value = "completed"
        run_mock.started_at = None
        run_mock.id = "run-drain-1"
        run_mock.duration_ms = 1000
        run_mock.input_tokens = 100
        run_mock.output_tokens = 50
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="anthropic/claude-sonnet-4.6",
            tools_allowed=["read_file", "spawn_agent", "list_tasks", "update_task"],
            instruction_file="brain/SOUL.md",
            worker=WorkerConfig(
                cron_expr="0 7-22/2 * * *",
                instruction_file="brain/WORKER.md",
                session_target="persistent",
                max_iterations=30,
                timeout_seconds=900,
                cost_budget_usd=2.0,
                persistent_history_limit=6,
                delivery_mode=DeliveryMode.ANNOUNCE,
                delivery_channel="telegram",
                delivery_to="99999999",
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
            await scheduler._run_worker("main")

        runner.execute.assert_called_once()
        call_kwargs = runner.execute.call_args.kwargs
        override = call_kwargs["agent_config"]

        # Worker overrides
        assert override.instruction_file == "brain/WORKER.md"
        assert override.max_iterations == 30
        assert override.delivery_mode == DeliveryMode.ANNOUNCE
        assert override.delivery_to == "99999999"
        assert override.cron_expr == "0 7-22/2 * * *"
        # Full tool inheritance from parent (no worker_tools_allowed override)
        assert override.tools_allowed == [
            "read_file",
            "spawn_agent",
            "list_tasks",
            "update_task",
        ]
        # Inherits model from parent
        assert override.model_primary == "anthropic/claude-sonnet-4.6"

        # trigger_detail + dedup_key use worker: prefix and :worker suffix
        assert call_kwargs["trigger_detail"] == "worker:0 7-22/2 * * *"

    @pytest.mark.asyncio
    async def test_worker_dedup_key_isolation(self, tmp_path):
        """Worker uses {agent_id}:worker dedup key — segregated from heartbeat."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = MagicMock()
        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            worker=WorkerConfig(
                cron_expr="0 */2 * * *",
                instruction_file="brain/WORKER.md",
            ),
        )

        acquire_calls = []

        def mock_acquire(key):
            acquire_calls.append(key)
            return False

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", side_effect=mock_acquire),
        ):
            await scheduler._run_worker("main")

        assert acquire_calls == ["main:worker"]

    @pytest.mark.asyncio
    async def test_worker_skipped_when_no_config(self, tmp_path):
        """_run_worker returns gracefully if agent has no worker config."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(id="main", name="Robothor", model_primary="test/model")

        with (
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
        ):
            await scheduler._run_worker("main")

        runner.execute.assert_not_called()

    def test_build_worker_config_restricts_tools_when_set(self):
        """When worker.tools_allowed is non-empty, it overrides parent's list.
        (Not currently used — worker defaults to full inheritance — but the
        hook should exist for future restrictions.)"""
        from robothor.engine.scheduler import _build_worker_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            tools_allowed=["read_file", "spawn_agent", "exec"],
            worker=WorkerConfig(
                cron_expr="0 */2 * * *",
                instruction_file="brain/WORKER.md",
                tools_allowed=["list_tasks", "update_task"],
            ),
        )
        built = _build_worker_config(parent)
        assert built.tools_allowed == ["list_tasks", "update_task"]

    def test_build_worker_config_inherits_tools_when_empty(self):
        """When worker.tools_allowed is empty, inherit parent's full list."""
        from robothor.engine.scheduler import _build_worker_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            tools_allowed=["read_file", "spawn_agent", "exec"],
            worker=WorkerConfig(
                cron_expr="0 */2 * * *",
                instruction_file="brain/WORKER.md",
            ),
        )
        built = _build_worker_config(parent)
        assert built.tools_allowed == ["read_file", "spawn_agent", "exec"]


class TestScoutToolRestriction:
    """Scout beat gets a restricted tool allowlist (no spawn/exec)."""

    def test_build_heartbeat_config_restricts_tools_when_set(self):
        """heartbeat.tools_allowed overrides parent tools_allowed."""
        from robothor.engine.scheduler import _build_heartbeat_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            tools_allowed=["read_file", "spawn_agent", "exec", "create_task"],
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
                tools_allowed=["read_file", "create_task", "list_tasks"],
            ),
        )
        built = _build_heartbeat_config(parent)
        assert built.tools_allowed == ["read_file", "create_task", "list_tasks"]
        assert "spawn_agent" not in built.tools_allowed
        assert "exec" not in built.tools_allowed

    def test_build_heartbeat_config_inherits_tools_when_empty(self):
        """Empty heartbeat.tools_allowed → inherit parent's full list (back-compat)."""
        from robothor.engine.scheduler import _build_heartbeat_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            tools_allowed=["read_file", "spawn_agent", "exec", "create_task"],
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
            ),
        )
        built = _build_heartbeat_config(parent)
        assert built.tools_allowed == ["read_file", "spawn_agent", "exec", "create_task"]


class TestScoutAuthorship:
    """Heartbeat runs attribute filed tasks to the scout identity."""

    def test_build_heartbeat_config_exposes_authorship_override(self):
        """_build_heartbeat_config surfaces hb.task_authorship_agent on the
        override AgentConfig as `task_author_override` — the runner then
        threads this to tool dispatch."""
        from robothor.engine.scheduler import _build_heartbeat_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
                task_authorship_agent="scout",
            ),
        )
        built = _build_heartbeat_config(parent)
        assert built.task_author_override == "scout"

    def test_build_heartbeat_config_no_override_by_default(self):
        """Without task_authorship_agent, task_author_override is empty."""
        from robothor.engine.scheduler import _build_heartbeat_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
            ),
        )
        built = _build_heartbeat_config(parent)
        assert built.task_author_override == ""

    def test_build_worker_config_no_authorship_override(self):
        """Worker/drain runs attribute tasks to 'main' (no override)."""
        from robothor.engine.scheduler import _build_worker_config

        parent = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            worker=WorkerConfig(
                cron_expr="0 */2 * * *",
                instruction_file="brain/WORKER.md",
            ),
        )
        built = _build_worker_config(parent)
        assert built.task_author_override == ""


class TestFollowupResurfacePhase0:
    """Both heartbeat and worker call resurface_due_followups() before
    reading the task queue — defined in this single phase-0 hook."""

    @pytest.mark.asyncio
    async def test_heartbeat_calls_resurface_before_scheduled(self, tmp_path):
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        run_mock = MagicMock()
        run_mock.status.value = "completed"
        run_mock.started_at = None
        run_mock.id = "run-1"
        run_mock.duration_ms = 100
        run_mock.input_tokens = 0
        run_mock.output_tokens = 0
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
            ),
        )

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch("robothor.crm.dal.resurface_due_followups", return_value=[]) as mock_resurface,
        ):
            await scheduler._run_heartbeat("main")

        mock_resurface.assert_called_once()

    @pytest.mark.asyncio
    async def test_worker_calls_resurface_before_scheduled(self, tmp_path):
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        run_mock = MagicMock()
        run_mock.status.value = "completed"
        run_mock.started_at = None
        run_mock.id = "run-2"
        run_mock.duration_ms = 100
        run_mock.input_tokens = 0
        run_mock.output_tokens = 0
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            worker=WorkerConfig(
                cron_expr="0 */2 * * *",
                instruction_file="brain/WORKER.md",
            ),
        )

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch(
                "robothor.crm.dal.resurface_due_followups", return_value=["task-a"]
            ) as mock_resurface,
        ):
            await scheduler._run_worker("main")

        mock_resurface.assert_called_once()

    @pytest.mark.asyncio
    async def test_resurface_failure_does_not_break_beat(self, tmp_path):
        """If resurface_due_followups raises, the beat still runs."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        runner = AsyncMock()
        run_mock = MagicMock()
        run_mock.status.value = "completed"
        run_mock.started_at = None
        run_mock.id = "run-3"
        run_mock.duration_ms = 100
        run_mock.input_tokens = 0
        run_mock.output_tokens = 0
        runner.execute = AsyncMock(return_value=run_mock)

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, runner)

        parent_config = AgentConfig(
            id="main",
            name="Robothor",
            model_primary="test/model",
            heartbeat=HeartbeatConfig(
                cron_expr="0 * * * *",
                instruction_file="brain/HEARTBEAT.md",
            ),
        )

        with (
            patch("robothor.engine.config.load_agent_config", return_value=parent_config),
            patch("robothor.engine.scheduler.try_acquire", return_value=True),
            patch("robothor.engine.scheduler.release"),
            patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch(
                "robothor.crm.dal.resurface_due_followups",
                side_effect=RuntimeError("db is down"),
            ),
        ):
            await scheduler._run_heartbeat("main")
        # Runner still executed despite the resurface failure.
        runner.execute.assert_called_once()


class TestSchedulerLeadershipGate:
    """The HA leadership gate at the top of _run_scheduled — only the leader
    replica fires scheduled jobs."""

    @pytest.mark.asyncio
    async def test_non_leader_does_not_fire(self, monkeypatch):
        import robothor.engine.scheduler as sched_mod
        from robothor.engine.scheduler import CronScheduler

        s = CronScheduler.__new__(CronScheduler)
        s.config = MagicMock(tenant_id="default")
        s.runner = MagicMock()
        acquire = AsyncMock(return_value=True)
        monkeypatch.setattr(sched_mod, "try_acquire", acquire)
        monkeypatch.setattr("robothor.engine.leader.is_leader", lambda: False)

        await s._run_scheduled("agent-1", "agent-1", MagicMock(), "cron")
        acquire.assert_not_called()  # gated out before dedup/execute

    @pytest.mark.asyncio
    async def test_leader_passes_gate(self, monkeypatch):
        import robothor.engine.scheduler as sched_mod
        from robothor.engine.scheduler import CronScheduler

        s = CronScheduler.__new__(CronScheduler)
        s.config = MagicMock(tenant_id="default")
        s.runner = MagicMock()
        # Leader passes the gate, then dedup short-circuits (already running).
        acquire = AsyncMock(return_value=False)
        monkeypatch.setattr(sched_mod, "try_acquire", acquire)
        monkeypatch.setattr("robothor.engine.leader.is_leader", lambda: True)

        await s._run_scheduled("agent-1", "agent-1", MagicMock(), "cron")
        acquire.assert_called_once()  # gate passed, reached dedup


class TestWorkflowCronParity:
    """Workflow cron registration is loud and parity-checked.

    A workflow whose cron never registers has NO trigger at all — it simply
    never runs again. The only prior evidence was the gap between two log
    lines ("Loaded 5 workflows" vs "Loaded 4 workflow cron jobs"), which
    named nothing and paged nobody.
    """

    @staticmethod
    def _scheduler(tmp_path, workflows):
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        wf_engine = MagicMock()
        wf_engine.get_workflows_for_cron.return_value = [(wf, wf.triggers[0]) for wf in workflows]
        scheduler = CronScheduler(config, MagicMock(), workflow_engine=wf_engine)
        scheduler.scheduler = MagicMock()
        return scheduler

    @staticmethod
    def _wf(wf_id: str, cron: str, timezone: str = "UTC"):
        from robothor.engine.models import WorkflowDef, WorkflowTriggerDef

        return WorkflowDef(
            id=wf_id,
            name=wf_id,
            triggers=[WorkflowTriggerDef(type="cron", cron=cron, timezone=timezone)],
        )

    async def test_add_job_failure_named_in_error_and_alert(self, tmp_path, caplog):
        """A workflow whose add_job raises is named in the ERROR and the alert."""
        import logging

        scheduler = self._scheduler(
            tmp_path,
            [
                self._wf("vision-pipeline", "12 6-22/6 * * *"),
                self._wf("email-pipeline", "*/5 * * * *"),
            ],
        )

        def _add_job(*args, **kwargs):
            if kwargs.get("id") == "workflow:vision-pipeline":
                raise RuntimeError("jobstore rejected the job")

        scheduler.scheduler.add_job.side_effect = _add_job

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            registered = await scheduler._register_workflow_cron_jobs()

        # The healthy workflow still registered — one failure must not abort the loop.
        assert registered == 1
        registered_ids = {
            call.kwargs.get("id") for call in scheduler.scheduler.add_job.call_args_list
        }
        assert "workflow:email-pipeline" in registered_ids

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("vision-pipeline" in m for m in errors), errors
        assert any("jobstore rejected the job" in m for m in errors), errors

        mock_alert.assert_awaited()
        level, title, body = mock_alert.await_args.args[:3]
        assert level == "warning"
        assert "vision-pipeline" in f"{title} {body}"

    async def test_invalid_cron_named_in_error_and_alert(self, tmp_path, caplog):
        """An uninterpretable cron expression is reported by workflow id."""
        import logging

        scheduler = self._scheduler(
            tmp_path,
            [
                self._wf("vision-pipeline", "not a cron"),
                self._wf("email-pipeline", "*/5 * * * *"),
            ],
        )

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            registered = await scheduler._register_workflow_cron_jobs()

        assert registered == 1
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("vision-pipeline" in m for m in errors), errors
        mock_alert.assert_awaited()
        assert "vision-pipeline" in " ".join(str(a) for a in mock_alert.await_args.args)

    async def test_happy_path_emits_no_parity_error(self, tmp_path, caplog):
        """All crons registering means no ERROR and no alert."""
        import logging

        scheduler = self._scheduler(
            tmp_path,
            [
                self._wf("vision-pipeline", "12 6-22/6 * * *"),
                self._wf("email-pipeline", "*/5 * * * *"),
            ],
        )

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            registered = await scheduler._register_workflow_cron_jobs()

        assert registered == 2
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
        mock_alert.assert_not_awaited()

    async def test_per_workflow_info_line_has_id_cron_and_next_fire(self, tmp_path, caplog):
        """One INFO line per registered workflow: id, cron expression, next fire."""
        import logging

        scheduler = self._scheduler(
            tmp_path,
            [
                self._wf("vision-pipeline", "12 6-22/6 * * *"),
                self._wf("email-pipeline", "*/5 * * * *"),
            ],
        )

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            await scheduler._register_workflow_cron_jobs()

        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        vision = [m for m in infos if "vision-pipeline" in m and "12 6-22/6 * * *" in m]
        email = [m for m in infos if "email-pipeline" in m and "*/5 * * * *" in m]
        assert len(vision) == 1, infos
        assert len(email) == 1, infos
        # Next fire time is on the line, not left to the operator to compute.
        assert "next_fire=20" in vision[0], vision[0]

    async def test_no_workflow_engine_is_a_noop(self, tmp_path):
        """No workflow engine configured — nothing registered, nothing alerted."""
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        scheduler = CronScheduler(config, MagicMock())
        scheduler.scheduler = MagicMock()

        with patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert:
            assert await scheduler._register_workflow_cron_jobs() == 0
        mock_alert.assert_not_awaited()
        scheduler.scheduler.add_job.assert_not_called()

    async def test_enumeration_failure_alerts_instead_of_crashing_boot(self, tmp_path, caplog):
        """If the workflow engine cannot be enumerated, alert — don't boot silently."""
        import logging

        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        wf_engine = MagicMock()
        wf_engine.get_workflows_for_cron.side_effect = RuntimeError("workflow store unreadable")
        scheduler = CronScheduler(config, MagicMock(), workflow_engine=wf_engine)
        scheduler.scheduler = MagicMock()

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            assert await scheduler._register_workflow_cron_jobs() == 0

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("workflow store unreadable" in m for m in errors), errors
        mock_alert.assert_awaited()
        assert mock_alert.await_args.args[0] == "warning"

    async def test_partially_registered_workflow_still_reported(self, tmp_path, caplog):
        """A workflow whose *second* cron trigger fails is still reported.

        The job id is per-workflow, so the first trigger to register claims it
        and a later failure leaves the workflow present but short a trigger —
        a lost run window that the set-level parity check alone would miss.
        """
        import logging

        from robothor.engine.config import EngineConfig
        from robothor.engine.models import WorkflowDef, WorkflowTriggerDef
        from robothor.engine.scheduler import CronScheduler

        wf = WorkflowDef(
            id="vision-pipeline",
            name="vision-pipeline",
            triggers=[
                WorkflowTriggerDef(type="cron", cron="12 6-22/6 * * *", timezone="UTC"),
                WorkflowTriggerDef(type="cron", cron="0 3 * * *", timezone="UTC"),
            ],
        )
        config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path)
        wf_engine = MagicMock()
        wf_engine.get_workflows_for_cron.return_value = [(wf, t) for t in wf.triggers]
        scheduler = CronScheduler(config, MagicMock(), workflow_engine=wf_engine)
        scheduler.scheduler = MagicMock()

        seen: set[str] = set()

        def _add_job(*args, **kwargs):
            job_id = kwargs.get("id")
            if job_id in seen:
                raise ValueError(f"Job identifier ({job_id}) conflicts with an existing job")
            seen.add(job_id)

        scheduler.scheduler.add_job.side_effect = _add_job

        with (
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.scheduler"),
        ):
            await scheduler._register_workflow_cron_jobs()

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("vision-pipeline" in m and "0 3 * * *" in m for m in errors), errors
        mock_alert.assert_awaited()
        assert "vision-pipeline" in " ".join(str(a) for a in mock_alert.await_args.args)


# ── The 2026-08-23 incident ──────────────────────────────────────────────────
# A YAML indentation error in docs/agents/main.yaml made it unparseable. The
# engine stayed active (running). Five minutes later the watchdog's reconcile
# rebuilt active_ids from only the manifests that parsed and DELETED main's
# schedules, at logger.info:
#
#   09:41:09 Reconcile: removing orphaned job main:worker
#   09:41:09 Reconcile: removing orphaned job main:heartbeat
#   09:41:09 Watchdog: reconciled schedules, pruned: ['main:worker', 'main:heartbeat']
#
# The operator got silence for 3h48m. agent_schedules has no tombstone, so the
# 13->11 delta was unrecoverable the moment delete_stale_schedules returned.
#
# Prune only from a clean, complete read. Partial knowledge is not authority to
# delete.

BROKEN_YAML = """\
id: main
name: Robothor
heartbeat:
  model:
    fallbacks:
      - a
    - b
"""


@pytest.fixture
def _reconcile_manifests(tmp_path):
    """A healthy two-agent fleet: main (heartbeat + worker) and worker."""
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "main.yaml").write_text(
        'id: main\nname: Robothor\nheartbeat:\n  cron: "0 12 * * *"\nworker:\n  cron: "0 7 * * *"\n'
    )
    (manifest_dir / "worker.yaml").write_text(
        'id: worker\nname: Worker\nschedule:\n  cron: "0 * * * *"\n'
    )
    return manifest_dir


def _scheduler_for(manifest_dir):
    from robothor.engine.config import EngineConfig
    from robothor.engine.scheduler import CronScheduler

    config = EngineConfig(manifest_dir=manifest_dir, workspace=manifest_dir.parent.parent)
    return CronScheduler(config, MagicMock())


def _job(job_id):
    job = MagicMock()
    job.id = job_id
    return job


class TestReconcileRefusesOnParseFailure:
    def test_reconcile_does_not_prune_when_a_manifest_fails_to_parse(self, _reconcile_manifests):
        """THE incident test.

        A healthy fleet, then main.yaml breaks. Nothing may be deleted — not
        the DB rows, not the in-memory jobs. The delete is one-way and the
        information needed to undo it does not exist.
        """
        scheduler = _scheduler_for(_reconcile_manifests)
        (_reconcile_manifests / "main.yaml").write_text(BROKEN_YAML)

        hb, worker_job = _job("main:heartbeat"), _job("main:worker")
        scheduler.scheduler.get_jobs = MagicMock(return_value=[hb, worker_job])

        mock_delete = MagicMock(return_value=[])
        with patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete):
            pruned = scheduler.reconcile_schedules()

        assert pruned == []
        mock_delete.assert_not_called()
        hb.remove.assert_not_called()
        worker_job.remove.assert_not_called()

    def test_reconcile_still_prunes_on_a_clean_scan(self, _reconcile_manifests):
        """The interlock must not turn reconcile into a no-op. An agent the
        operator really deleted still gets cleaned up."""
        scheduler = _scheduler_for(_reconcile_manifests)
        (_reconcile_manifests / "main.yaml").unlink()  # deliberately removed

        stale = _job("main:heartbeat")
        legit = _job("worker")
        scheduler.scheduler.get_jobs = MagicMock(return_value=[stale, legit])

        with patch("robothor.engine.scheduler.delete_stale_schedules", MagicMock(return_value=[])):
            pruned = scheduler.reconcile_schedules()

        stale.remove.assert_called_once()
        legit.remove.assert_not_called()
        assert "main:heartbeat" in pruned

    def test_reconcile_refuses_when_the_manifest_dir_is_unreadable(self, tmp_path):
        """One NFS hiccup or a stray chmod must not empty the job registry.

        delete_stale_schedules happens to guard the DB half against an empty
        active_ids; the in-memory loop had no such guard and would remove
        EVERY job while the engine stayed active (running).
        """
        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=tmp_path / "gone", workspace=tmp_path)
        scheduler = CronScheduler(config, MagicMock())

        hb = _job("main:heartbeat")
        scheduler.scheduler.get_jobs = MagicMock(return_value=[hb])

        mock_delete = MagicMock(return_value=[])
        with patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete):
            pruned = scheduler.reconcile_schedules()

        assert pruned == []
        mock_delete.assert_not_called()
        hb.remove.assert_not_called()


class TestReconcileKeepsSystemJobs:
    """Engine-owned infrastructure jobs are not agent schedules.

    reconcile exempted only `workflow:`, so memory:write-job-sweeper (an
    interval job registered in start()) was culled on the first reconcile after
    EVERY restart — confirmed live on 2026-08-23 at 13:31:58, five minutes
    after a restart. The sweeper ran for at most five minutes per engine
    lifetime.
    """

    @pytest.mark.parametrize(
        "job_id",
        ["memory:write-job-sweeper", "memory:vault-projection", "workflow:email-pipeline"],
    )
    def test_system_jobs_survive_reconcile(self, _reconcile_manifests, job_id):
        scheduler = _scheduler_for(_reconcile_manifests)
        system_job = _job(job_id)
        scheduler.scheduler.get_jobs = MagicMock(return_value=[system_job])

        with patch("robothor.engine.scheduler.delete_stale_schedules", MagicMock(return_value=[])):
            pruned = scheduler.reconcile_schedules()

        system_job.remove.assert_not_called()
        assert job_id not in pruned

    def test_every_registered_job_id_is_agent_derived_or_system_prefixed(self):
        """Drift gate: the NEXT add_job must not re-introduce the bug.

        Any literal job id in scheduler.py that is not built from an agent id
        has to carry a namespace prefix reconcile knows to leave alone.
        """
        import re
        from pathlib import Path

        from robothor.engine.scheduler import _SYSTEM_JOB_PREFIXES

        source = Path(__import__("robothor.engine.scheduler", fromlist=["x"]).__file__).read_text()
        literals = re.findall(r'\bid=["\']([a-z][a-z0-9:_-]*)["\']', source)
        assert literals, "no literal job ids found — has add_job been refactored?"
        for job_id in literals:
            assert job_id.startswith(_SYSTEM_JOB_PREFIXES), (
                f"job id {job_id!r} is a literal that reconcile would treat as a "
                "stale agent schedule and delete. Give it a namespace prefix in "
                "_SYSTEM_JOB_PREFIXES."
            )


class TestReconcileAlwaysConsultsTheGuard:
    """The guard owns BOTH transitions, so it must be called unconditionally.

    Caught by a live probe, not by a unit test: reconcile() originally called
    alert_manifest_scan only when the scan was dirty, so the recovery notice
    could never fire and the dedup key was never cleared — a manifest fixed at
    14:31 left the guard armed indefinitely, and the next breakage of the same
    file would have been swallowed by the stale floor.

    test_recovery_sends_info_not_critical passed the whole time because it
    calls alert_manifest_scan directly. The caller was the inert part.
    """

    @pytest.mark.asyncio
    async def test_reconcile_consults_the_guard_on_a_clean_scan(self, _reconcile_manifests):
        scheduler = _scheduler_for(_reconcile_manifests)
        scheduler.scheduler.get_jobs = MagicMock(return_value=[])

        mock_guard = AsyncMock()
        with (
            patch("robothor.engine.scheduler.alert_manifest_scan", mock_guard),
            patch("robothor.engine.scheduler.delete_stale_schedules", MagicMock(return_value=[])),
        ):
            await scheduler.reconcile()

        mock_guard.assert_awaited_once()
        assert mock_guard.await_args[0][0].clean, "the guard must see the clean scan too"

    @pytest.mark.asyncio
    async def test_reconcile_consults_the_guard_on_a_dirty_scan(self, _reconcile_manifests):
        scheduler = _scheduler_for(_reconcile_manifests)
        scheduler.scheduler.get_jobs = MagicMock(return_value=[])
        (_reconcile_manifests / "main.yaml").write_text(BROKEN_YAML)

        mock_guard = AsyncMock()
        with (
            patch("robothor.engine.scheduler.alert_manifest_scan", mock_guard),
            patch("robothor.engine.scheduler.delete_stale_schedules", MagicMock(return_value=[])),
        ):
            await scheduler.reconcile()

        mock_guard.assert_awaited_once()
        assert not mock_guard.await_args[0][0].clean
