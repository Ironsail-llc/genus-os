"""Tests for auto-task CRM lifecycle — engine auto-creates/resolves tasks per agent run."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, AgentRun, RunStatus


class TestAutoTaskCreation:
    """Auto-task should create a CRM task when agent_config.auto_task=True."""

    @pytest.mark.asyncio
    async def test_auto_task_created_on_execute(self, engine_config):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(engine_config)

        config = AgentConfig(
            id="test-agent",
            name="Test Agent",
            model_primary="openrouter/z-ai/glm-5",
            auto_task=True,
            max_iterations=1,
            timeout_seconds=10,
        )
        mock_create_task = MagicMock(return_value="task-uuid-123")

        with (
            patch("robothor.engine.config.load_agent_config", return_value=config),
            patch("robothor.engine.tracking.create_run"),
            patch("robothor.engine.tracking.update_run"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.create_task", mock_create_task),
            patch("robothor.engine.model_registry.compute_token_budget", return_value=50000),
            patch("robothor.engine.runner.AgentRunner._run_loop", return_value=None),
        ):
            await runner.execute("test-agent", "do stuff", agent_config=config)
            mock_create_task.assert_called_once()
            call_kwargs = mock_create_task.call_args.kwargs
            assert call_kwargs["status"] == "IN_PROGRESS"
            assert call_kwargs["assigned_to_agent"] == "test-agent"
            assert call_kwargs["created_by_agent"] == "engine"
            assert "auto" in call_kwargs["tags"]

    @pytest.mark.asyncio
    async def test_auto_task_not_created_when_disabled(self, engine_config):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(engine_config)

        config = AgentConfig(
            id="test-agent",
            name="Test Agent",
            model_primary="openrouter/z-ai/glm-5",
            auto_task=False,
            max_iterations=1,
            timeout_seconds=10,
        )
        mock_create_task = MagicMock()

        with (
            patch("robothor.engine.config.load_agent_config", return_value=config),
            patch("robothor.engine.tracking.create_run"),
            patch("robothor.engine.tracking.update_run"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.create_task", mock_create_task),
            patch("robothor.engine.model_registry.compute_token_budget", return_value=50000),
            patch("robothor.engine.runner.AgentRunner._run_loop", return_value=None),
        ):
            await runner.execute("test-agent", "do stuff", agent_config=config)
            mock_create_task.assert_not_called()


class TestAutoTaskResolution:
    """Auto-task should resolve CRM task based on run outcome."""

    def test_finish_run_resolves_on_success(self, engine_config):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(engine_config)

        run = AgentRun(
            agent_id="test-agent",
            status=RunStatus.COMPLETED,
            task_id="task-uuid-123",
            output_text="All done!",
        )

        mock_resolve = MagicMock(return_value=True)
        with (
            patch("robothor.engine.tracking.update_run"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.resolve_task", mock_resolve),
        ):
            runner._finish_run(run)
            mock_resolve.assert_called_once_with(
                "task-uuid-123",
                resolution="Run completed: All done!",
                agent_id="test-agent",
            )

    def test_finish_run_sets_todo_on_failure(self, engine_config):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(engine_config)

        run = AgentRun(
            agent_id="test-agent",
            status=RunStatus.FAILED,
            task_id="task-uuid-456",
        )

        mock_update = MagicMock(return_value=True)
        with (
            patch("robothor.engine.tracking.update_run"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.update_task", mock_update),
        ):
            runner._finish_run(run)
            mock_update.assert_called_once()
            call_kwargs = mock_update.call_args.kwargs
            assert call_kwargs["status"] == "TODO"
            assert "failed" in call_kwargs["tags"]

    def test_finish_run_no_task_id_skips_resolution(self, engine_config):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(engine_config)

        run = AgentRun(
            agent_id="test-agent",
            status=RunStatus.COMPLETED,
            task_id=None,
        )

        mock_resolve = MagicMock()
        with (
            patch("robothor.engine.tracking.update_run"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.resolve_task", mock_resolve),
        ):
            runner._finish_run(run)
            mock_resolve.assert_not_called()


class TestAutoTaskConfig:
    """auto_task field should be parsed from YAML manifest."""

    def test_auto_task_parsed_from_manifest(self):
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "test",
            "name": "Test",
            "auto_task": True,
        }
        config = manifest_to_agent_config(manifest)
        assert config.auto_task is True

    def test_auto_task_defaults_false(self):
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "test",
            "name": "Test",
        }
        config = manifest_to_agent_config(manifest)
        assert config.auto_task is False
