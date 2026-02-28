"""Tests for Phase 4 — expanded triggers, downstream chains, cost endpoint, engine-report."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.dedup import clear as dedup_clear
from robothor.engine.models import AgentRun, RunStatus


@pytest.fixture(autouse=True)
def clean_dedup():
    dedup_clear()
    yield
    dedup_clear()


class TestExpandedTriggers:
    def test_all_expected_streams_registered(self):
        from robothor.engine.hooks import EVENT_TRIGGERS

        assert "email" in EVENT_TRIGGERS
        assert "calendar" in EVENT_TRIGGERS
        assert "vision" in EVENT_TRIGGERS

    def test_vision_person_unknown_trigger(self):
        from robothor.engine.hooks import EVENT_TRIGGERS

        triggers = EVENT_TRIGGERS["vision"]
        unknown = [t for t in triggers if t["event_type"] == "vision.person_unknown"]
        assert len(unknown) == 1
        assert unknown[0]["agent_id"] == "vision-monitor"


class TestDownstreamAgentChains:
    @pytest.mark.asyncio
    async def test_downstream_triggered_on_success(self, engine_config, sample_agent_config):
        import asyncio as _asyncio

        import robothor.engine.scheduler as sched_module
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        sample_agent_config.downstream_agents = ["email-analyst"]

        run = AgentRun(agent_id="test-agent", status=RunStatus.COMPLETED)
        run.started_at = None
        run.duration_ms = 100
        run.input_tokens = 10
        run.output_tokens = 5

        created_coros = []

        def track_create_task(coro, **kwargs):
            created_coros.append(True)
            # Cancel the coroutine to prevent it from actually running
            coro.close()
            # Return a done future
            fut = _asyncio.get_event_loop().create_future()
            fut.set_result(None)
            return fut

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch("robothor.engine.tracking.get_schedule", return_value={"consecutive_errors": 0}),
            patch("robothor.engine.tracking.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch("robothor.engine.delivery.deliver", new_callable=AsyncMock),
            patch.object(runner, "execute", new_callable=AsyncMock, return_value=run),
            patch.object(sched_module.asyncio, "create_task", side_effect=track_create_task),
        ):
            await scheduler._run_agent("test-agent")

        assert len(created_coros) == 1

    @pytest.mark.asyncio
    async def test_no_downstream_on_failure(self, engine_config, sample_agent_config):
        import asyncio as _asyncio

        import robothor.engine.scheduler as sched_module
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        sample_agent_config.downstream_agents = ["email-analyst"]

        run = AgentRun(agent_id="test-agent", status=RunStatus.FAILED)
        run.started_at = None
        run.duration_ms = 100
        run.input_tokens = 10
        run.output_tokens = 5
        run.error_message = "test error"

        created_coros = []

        def track_create_task(coro, **kwargs):
            created_coros.append(True)
            coro.close()
            fut = _asyncio.get_event_loop().create_future()
            fut.set_result(None)
            return fut

        with (
            patch("robothor.engine.config.load_agent_config", return_value=sample_agent_config),
            patch("robothor.engine.tracking.get_schedule", return_value={"consecutive_errors": 0}),
            patch("robothor.engine.tracking.update_schedule_state"),
            patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
            patch("robothor.engine.delivery.deliver", new_callable=AsyncMock),
            patch.object(runner, "execute", new_callable=AsyncMock, return_value=run),
            patch.object(sched_module.asyncio, "create_task", side_effect=track_create_task),
        ):
            await scheduler._run_agent("test-agent")

        assert len(created_coros) == 0


class TestCostEndpoint:
    def test_cost_endpoint_exists(self, engine_config):
        from robothor.engine.health import create_health_app

        app = create_health_app(engine_config)
        routes = [r.path for r in app.routes]
        assert "/costs" in routes


class TestEngineReportManifest:
    def test_manifest_loads(self):
        from robothor.engine.config import load_manifest, manifest_to_agent_config

        manifest_path = (
            Path(__file__).parent.parent.parent.parent / "docs" / "agents" / "engine-report.yaml"
        )
        if not manifest_path.exists():
            pytest.skip("engine-report.yaml not found")
        data = load_manifest(manifest_path)
        assert data is not None
        config = manifest_to_agent_config(data)
        assert config.id == "engine-report"
        assert config.cron_expr == "0 23 * * *"
        assert config.instruction_file == "brain/ENGINE_REPORT.md"


class TestConfigParseWarmup:
    def test_warmup_fields_parsed(self):
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "test",
            "name": "Test",
            "warmup": {
                "memory_blocks": ["findings"],
                "context_files": ["status.md"],
                "peer_agents": ["peer-1"],
            },
            "downstream_agents": ["agent-2"],
        }
        config = manifest_to_agent_config(manifest)
        assert config.warmup_memory_blocks == ["findings"]
        assert config.warmup_context_files == ["status.md"]
        assert config.warmup_peer_agents == ["peer-1"]
        assert config.downstream_agents == ["agent-2"]

    def test_warmup_defaults_empty(self):
        from robothor.engine.config import manifest_to_agent_config

        manifest = {"id": "test", "name": "Test"}
        config = manifest_to_agent_config(manifest)
        assert config.warmup_memory_blocks == []
        assert config.warmup_context_files == []
        assert config.warmup_peer_agents == []
        assert config.downstream_agents == []
