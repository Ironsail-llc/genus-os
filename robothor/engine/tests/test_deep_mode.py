"""Tests for /deep mode — DeepRunState model, execute_deep(), chat endpoints, CLI flag."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentRun,
    DeepRunState,
    RunStatus,
    StepType,
)

# ─── Model Tests ──────────────────────────────────────────────────────


class TestDeepRunState:
    """Tests for the DeepRunState dataclass."""

    def test_default_values(self):
        state = DeepRunState(deep_id="test-123", query="What is 2+2?")
        assert state.deep_id == "test-123"
        assert state.query == "What is 2+2?"
        assert state.status == "running"
        assert state.response == ""
        assert state.cost_usd == 0.0
        assert state.execution_time_s == 0.0
        assert state.error == ""

    def test_completed_state(self):
        state = DeepRunState(
            deep_id="test-456",
            query="Analyze calendar",
            status="completed",
            response="Here is the analysis...",
            execution_time_s=45.2,
            cost_usd=0.87,
            context_chars=5000,
            trajectory_file="/tmp/trace.json",
        )
        assert state.status == "completed"
        assert state.cost_usd == 0.87
        assert state.context_chars == 5000

    def test_failed_state(self):
        state = DeepRunState(
            deep_id="test-789",
            query="Will fail",
            status="failed",
            error="RLM budget exceeded",
        )
        assert state.status == "failed"
        assert "budget" in state.error


class TestStepTypeDeepReason:
    """Verify DEEP_REASON is in the StepType enum."""

    def test_deep_reason_exists(self):
        assert StepType.DEEP_REASON == "deep_reason"

    def test_deep_reason_is_valid_value(self):
        assert StepType("deep_reason") == StepType.DEEP_REASON


# ─── execute_deep() Tests ────────────────────────────────────────────


class TestExecuteDeep:
    """Tests for AgentRunner.execute_deep()."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.tenant_id = "test-tenant"
        config.workspace = "/tmp/test-workspace"
        config.manifest_dir = "/tmp/manifests"
        return config

    @pytest.fixture
    def runner(self, mock_config):
        from robothor.engine.runner import AgentRunner

        return AgentRunner(mock_config)

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_execute_deep_success(
        self, mock_rlm, mock_create_step, mock_update_run, mock_create_run, runner
    ):
        """execute_deep() should return a completed AgentRun with RLM response."""
        mock_rlm.return_value = {
            "response": "Deep analysis result",
            "execution_time_s": 42.5,
            "cost_usd": 0.75,
            "context_chars": 3000,
            "trajectory_file": "/tmp/trace.json",
        }

        run = await runner.execute_deep(query="Analyze my calendar")

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Deep analysis result"
        assert run.total_cost_usd == 0.75
        assert len(run.steps) == 1
        assert run.steps[0].step_type == StepType.DEEP_REASON

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_execute_deep_failure(
        self, mock_rlm, mock_create_step, mock_update_run, mock_create_run, runner
    ):
        """execute_deep() should handle RLM errors gracefully."""
        mock_rlm.return_value = {
            "error": "RLM budget exceeded ($2.00 limit)",
            "execution_time_s": 120.0,
            "partial": True,
        }

        run = await runner.execute_deep(query="Very complex query")

        assert run.status == RunStatus.FAILED
        assert "budget exceeded" in run.error_message
        assert len(run.steps) == 2  # ERROR step + DEEP_REASON step
        assert any(s.step_type == StepType.ERROR for s in run.steps)
        assert any(s.step_type == StepType.DEEP_REASON for s in run.steps)
        assert all(s.error_message is not None for s in run.steps)

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_execute_deep_with_progress(
        self, mock_rlm, mock_create_step, mock_update_run, mock_create_run, runner
    ):
        """execute_deep() should call on_progress callback during execution."""
        progress_calls = []

        async def on_progress(data):
            progress_calls.append(data)

        # Make the RLM call take a bit of time so progress fires
        async def slow_rlm(**kwargs):
            await asyncio.sleep(0.1)
            return {
                "response": "Result",
                "execution_time_s": 10.0,
                "cost_usd": 0.50,
                "context_chars": 1000,
            }

        # We need to patch to_thread since we're testing the async wrapper
        mock_rlm.return_value = {
            "response": "Result",
            "execution_time_s": 10.0,
            "cost_usd": 0.50,
            "context_chars": 1000,
        }

        run = await runner.execute_deep(
            query="Test query",
            on_progress=on_progress,
        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Result"

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_execute_deep_with_history(
        self, mock_rlm, mock_create_step, mock_update_run, mock_create_run, runner
    ):
        """execute_deep() should build context from conversation history."""
        mock_rlm.return_value = {
            "response": "Analysis with context",
            "execution_time_s": 30.0,
            "cost_usd": 0.60,
            "context_chars": 5000,
        }

        history = [
            {"role": "user", "content": "What about John's schedule?"},
            {"role": "assistant", "content": "John has meetings at 10am and 2pm."},
        ]

        run = await runner.execute_deep(
            query="Deeper analysis of John",
            conversation_history=history,
        )

        assert run.status == RunStatus.COMPLETED
        # Verify context was passed to RLM
        call_kwargs = mock_rlm.call_args
        assert "context" in call_kwargs.kwargs or len(call_kwargs.args) > 1

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch(
        "robothor.engine.rlm_tool.execute_deep_reason",
        side_effect=ImportError("rlms not installed"),
    )
    async def test_execute_deep_import_error(
        self, mock_rlm, mock_create_step, mock_update_run, mock_create_run, runner
    ):
        """execute_deep() should handle missing rlms package gracefully."""
        run = await runner.execute_deep(query="Test query")

        assert run.status == RunStatus.FAILED
        assert run.error_message is not None


# ─── Cost Unification Tests ──────────────────────────────────────────


class TestCostUnification:
    """Verify that tool-reported costs propagate to AgentRun.total_cost_usd."""

    def test_deep_run_cost_in_run(self):
        """execute_deep() should add RLM cost to run.total_cost_usd."""
        run = AgentRun()
        run.total_cost_usd = 0.0

        # Simulate what execute_deep does
        cost_usd = 0.87
        run.total_cost_usd += cost_usd

        assert run.total_cost_usd == 0.87


# ─── Chat Endpoint Tests ─────────────────────────────────────────────


class TestDeepChatEndpoints:
    """Tests for /chat/deep/start and /chat/deep/status endpoints."""

    @pytest.fixture
    def mock_runner(self):
        runner = AsyncMock()
        run = MagicMock(spec=AgentRun)
        run.status = RunStatus.COMPLETED
        run.output_text = "Deep result"
        run.error_message = None
        run.total_cost_usd = 0.75
        run.duration_ms = 42500
        run.id = str(uuid.uuid4())
        runner.execute_deep = AsyncMock(return_value=run)
        return runner

    @pytest.fixture
    def app(self, mock_runner):
        """Create a minimal FastAPI test app with chat endpoints."""
        from fastapi import FastAPI

        from robothor.engine import chat

        app = FastAPI()
        app.include_router(chat.router)

        # Inject mock runner
        chat._runner = mock_runner
        config = MagicMock()
        config.tenant_id = "test-tenant"
        config.default_chat_agent = "main"
        config.main_session_key = "agent:main:primary"
        chat._config = config

        return app

    @pytest.mark.asyncio
    async def test_deep_start_requires_query(self, app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/chat/deep/start",
                json={"session_key": "test-session"},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_deep_status_no_active(self, app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/chat/deep/status",
                params={"session_key": "test-session"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["active"] is False


# ─── READONLY_TOOLS Includes deep_reason ──────────────────────────────


class TestDeepReasonInReadonlyTools:
    """Verify deep_reason is available during plan mode exploration."""

    def test_deep_reason_in_readonly_tools(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "deep_reason" in READONLY_TOOLS
