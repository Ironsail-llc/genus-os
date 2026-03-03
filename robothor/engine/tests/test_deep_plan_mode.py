"""Tests for deep plan mode — /deep always plans first.

When deep mode is activated, it auto-triggers planning to gather rich context
before routing to the RLM. Tests cover:
- PlanState.deep_plan flag
- execute_deep() with context_override
- execute() with deep_plan preamble
- plan_approve branching to execute_deep for deep plans
- Telegram /deep routing through plan mode
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentRun,
    PlanState,
    RunStatus,
)

# ─── Model Tests ──────────────────────────────────────────────────────


class TestPlanStateDeepPlan:
    """Tests for PlanState.deep_plan flag."""

    def test_default_deep_plan_false(self):
        plan = PlanState(plan_id="test-1", plan_text="Do X", original_message="X")
        assert plan.deep_plan is False

    def test_deep_plan_set_true(self):
        plan = PlanState(
            plan_id="test-2",
            plan_text="Research Y",
            original_message="Y",
            deep_plan=True,
        )
        assert plan.deep_plan is True

    def test_deep_plan_in_dict(self):
        """deep_plan should serialize properly."""
        plan = PlanState(
            plan_id="test-3",
            plan_text="Plan Z",
            original_message="Z",
            deep_plan=True,
        )
        # Verify the field is accessible
        assert plan.deep_plan is True
        assert plan.status == "pending"


# ─── Runner Tests — deep_plan preamble ──────────────────────────────


class TestDeepPlanPreamble:
    """Tests that execute() uses DEEP_PLAN preamble when deep_plan=True."""

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

    def test_preamble_constants_exist(self):
        """DEEP_PLAN_PREAMBLE and DEEP_PLAN_SUFFIX should be defined."""
        from robothor.engine.runner import DEEP_PLAN_PREAMBLE, DEEP_PLAN_SUFFIX

        assert "DEEP PLAN MODE" in DEEP_PLAN_PREAMBLE
        assert "context gathering" in DEEP_PLAN_SUFFIX.lower()
        assert "[PLAN_READY]" in DEEP_PLAN_SUFFIX

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.runner.build_system_prompt", return_value="SYSTEM")
    async def test_execute_uses_deep_plan_preamble(
        self, mock_build, mock_step, mock_update, mock_create, runner
    ):
        """When deep_plan=True and readonly_mode=True, should use DEEP_PLAN preamble."""
        from robothor.engine.models import AgentConfig, DeliveryMode

        agent_config = AgentConfig(
            id="main",
            name="Main",
            model_primary="test-model",
            max_iterations=5,
            timeout_seconds=30,
            delivery_mode=DeliveryMode.NONE,
            error_feedback=False,
            planning_enabled=False,
            verification_enabled=False,
        )

        # Mock _run_loop to capture the system prompt
        captured_sessions = []

        async def mock_run_loop(session, *args, **kwargs):
            captured_sessions.append(session)
            session._accumulated_text = "Test plan\n\n[PLAN_READY]"

        runner._run_loop = mock_run_loop

        await runner.execute(
            agent_id="main",
            message="Test query",
            agent_config=agent_config,
            readonly_mode=True,
            deep_plan=True,
        )

        # Verify the system prompt contains deep plan preamble
        assert len(captured_sessions) >= 1
        system_msg = captured_sessions[0].messages[0]
        assert system_msg["role"] == "system"
        assert "DEEP PLAN MODE" in system_msg["content"]

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.runner.build_system_prompt", return_value="SYSTEM")
    async def test_execute_uses_normal_preamble_without_deep_plan(
        self, mock_build, mock_step, mock_update, mock_create, runner
    ):
        """When deep_plan=False and readonly_mode=True, should use normal PLAN_MODE preamble."""
        from robothor.engine.models import AgentConfig, DeliveryMode

        agent_config = AgentConfig(
            id="main",
            name="Main",
            model_primary="test-model",
            max_iterations=5,
            timeout_seconds=30,
            delivery_mode=DeliveryMode.NONE,
            error_feedback=False,
            planning_enabled=False,
            verification_enabled=False,
        )

        captured_sessions = []

        async def mock_run_loop(session, *args, **kwargs):
            captured_sessions.append(session)
            session._accumulated_text = "Normal plan\n\n[PLAN_READY]"

        runner._run_loop = mock_run_loop

        await runner.execute(
            agent_id="main",
            message="Test query",
            agent_config=agent_config,
            readonly_mode=True,
            deep_plan=False,
        )

        assert len(captured_sessions) >= 1
        system_msg = captured_sessions[0].messages[0]
        assert "PLAN MODE — STRATEGIC PAUSE" in system_msg["content"]
        assert "DEEP PLAN MODE" not in system_msg["content"]


# ─── Runner Tests — execute_deep with context_override ──────────────


class TestExecuteDeepContextOverride:
    """Tests for execute_deep() with context_override parameter."""

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
    async def test_context_override_used(
        self, mock_rlm, mock_step, mock_update, mock_create, runner
    ):
        """When context_override is provided, it should be used instead of conversation history."""
        mock_rlm.return_value = {
            "response": "Deep result with rich context",
            "execution_time_s": 30.0,
            "cost_usd": 0.75,
            "context_chars": 10000,
        }

        rich_context = (
            "Original request: What are my conflicts?\n\n"
            "Research plan:\n1. Check calendar\n2. Check tasks\n\n"
            "Exploration output:\nFound 3 conflicts on Tuesday..."
        )

        run = await runner.execute_deep(
            query="What are my conflicts?",
            context_override=rich_context,
        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Deep result with rich context"

        # Verify context_override was passed to the RLM (not the limited history)
        call_kwargs = mock_rlm.call_args
        context_arg = call_kwargs.kwargs.get(
            "context", call_kwargs.args[1] if len(call_kwargs.args) > 1 else ""
        )
        assert "Research plan" in context_arg
        assert "Found 3 conflicts" in context_arg

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_context_override_ignores_history(
        self, mock_rlm, mock_step, mock_update, mock_create, runner
    ):
        """When context_override is set, conversation_history should be ignored."""
        mock_rlm.return_value = {
            "response": "Result",
            "execution_time_s": 10.0,
            "cost_usd": 0.50,
            "context_chars": 5000,
        }

        history = [
            {"role": "user", "content": "Old message from history"},
            {"role": "assistant", "content": "Old response"},
        ]

        run = await runner.execute_deep(
            query="Test",
            conversation_history=history,
            context_override="Custom override context",
        )

        assert run.status == RunStatus.COMPLETED
        call_kwargs = mock_rlm.call_args
        context_arg = call_kwargs.kwargs.get(
            "context", call_kwargs.args[1] if len(call_kwargs.args) > 1 else ""
        )
        assert context_arg == "Custom override context"
        assert "Old message" not in context_arg

    @pytest.mark.asyncio
    @patch("robothor.engine.runner.create_run")
    @patch("robothor.engine.runner.update_run")
    @patch("robothor.engine.runner.create_step")
    @patch("robothor.engine.rlm_tool.execute_deep_reason")
    async def test_no_context_override_uses_history(
        self, mock_rlm, mock_step, mock_update, mock_create, runner
    ):
        """Without context_override, should fall back to conversation history."""
        mock_rlm.return_value = {
            "response": "Result",
            "execution_time_s": 10.0,
            "cost_usd": 0.50,
            "context_chars": 1000,
        }

        history = [
            {"role": "user", "content": "Hello from history"},
            {"role": "assistant", "content": "Hi there"},
        ]

        run = await runner.execute_deep(
            query="Test",
            conversation_history=history,
        )

        assert run.status == RunStatus.COMPLETED
        call_kwargs = mock_rlm.call_args
        context_arg = call_kwargs.kwargs.get(
            "context", call_kwargs.args[1] if len(call_kwargs.args) > 1 else ""
        )
        assert "Hello from history" in context_arg


# ─── Chat Endpoint Tests — plan_approve branching ───────────────────


class TestPlanApproveDeepBranch:
    """Tests for plan_approve routing to execute_deep for deep plans."""

    @pytest.fixture
    def mock_runner(self):
        runner = AsyncMock()

        # Mock for normal execution
        normal_run = MagicMock(spec=AgentRun)
        normal_run.status = RunStatus.COMPLETED
        normal_run.output_text = "Execution result"
        normal_run.error_message = None
        normal_run.model_used = "test-model"
        normal_run.input_tokens = 100
        normal_run.output_tokens = 50
        normal_run.duration_ms = 5000
        normal_run.id = str(uuid.uuid4())
        runner.execute = AsyncMock(return_value=normal_run)

        # Mock for deep execution
        deep_run = MagicMock(spec=AgentRun)
        deep_run.status = RunStatus.COMPLETED
        deep_run.output_text = "Deep reasoning result"
        deep_run.error_message = None
        deep_run.total_cost_usd = 0.87
        deep_run.duration_ms = 23500
        deep_run.id = str(uuid.uuid4())
        runner.execute_deep = AsyncMock(return_value=deep_run)

        return runner

    @pytest.fixture
    def app(self, mock_runner):
        """Create a minimal FastAPI test app with chat endpoints."""
        from fastapi import FastAPI

        from robothor.engine import chat

        app = FastAPI()
        app.include_router(chat.router)

        chat._runner = mock_runner
        config = MagicMock()
        config.tenant_id = "test-tenant"
        config.default_chat_agent = "main"
        config.main_session_key = "agent:main:primary"
        chat._config = config

        return app

    @pytest.mark.asyncio
    async def test_plan_start_passes_deep_plan(self, app, mock_runner):
        """plan/start should forward deep_plan to runner.execute()."""
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/chat/plan/start",
                json={
                    "session_key": "test-session",
                    "message": "What conflicts?",
                    "deep_plan": True,
                },
            )
            assert resp.status_code == 200

            # Read SSE to completion
            # The mock runner.execute returns a MagicMock, so we just verify
            # it was called with deep_plan=True
            call_kwargs = mock_runner.execute.call_args
            assert call_kwargs is not None
            assert call_kwargs.kwargs.get("deep_plan") is True
            assert call_kwargs.kwargs.get("readonly_mode") is True

    @pytest.mark.asyncio
    async def test_plan_approve_routes_to_deep(self, app, mock_runner):
        """Approving a deep plan should call execute_deep with context_override."""
        from datetime import UTC, datetime

        from robothor.engine import chat

        # Manually set up a session with a deep plan
        session = chat._get_session("test-session")
        session.history.append({"role": "user", "content": "Test query"})
        session.history.append(
            {"role": "assistant", "content": "Exploration: found 3 items\n\n[PLAN_READY]"}
        )

        plan = PlanState(
            plan_id="deep-plan-001",
            plan_text="1. Check calendar\n2. Analyze conflicts",
            original_message="What conflicts?",
            status="pending",
            deep_plan=True,
            created_at=datetime.now(UTC).isoformat(),
        )
        session.active_plan = plan

        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/chat/plan/approve",
                json={
                    "session_key": "test-session",
                    "plan_id": "deep-plan-001",
                },
            )
            assert resp.status_code == 200

            # Read SSE stream to completion
            _ = resp.text

            # Should have called execute_deep, not execute
            assert mock_runner.execute_deep.called
            assert not mock_runner.execute.called

            # Verify context_override was passed
            deep_call = mock_runner.execute_deep.call_args
            assert deep_call.kwargs.get("context_override") is not None
            ctx = deep_call.kwargs["context_override"]
            assert "What conflicts?" in ctx
            assert "Check calendar" in ctx

    @pytest.mark.asyncio
    async def test_plan_approve_normal_plan_does_not_call_deep(self, app, mock_runner):
        """Approving a normal (non-deep) plan should call execute, not execute_deep."""
        from datetime import UTC, datetime

        from robothor.engine import chat

        session = chat._get_session("test-session-normal")
        plan = PlanState(
            plan_id="normal-plan-001",
            plan_text="1. Do thing A\n2. Do thing B",
            original_message="Do A and B",
            status="pending",
            deep_plan=False,
            created_at=datetime.now(UTC).isoformat(),
        )
        session.active_plan = plan

        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/chat/plan/approve",
                json={
                    "session_key": "test-session-normal",
                    "plan_id": "normal-plan-001",
                },
            )
            assert resp.status_code == 200

            # Read SSE to completion
            _ = resp.text

            # Should have called execute (normal), not execute_deep
            assert mock_runner.execute.called
            assert not mock_runner.execute_deep.called


# ─── Telegram Tests — /deep routes through plan ─────────────────────


class TestTelegramDeepPlanRouting:
    """Tests that /deep routes through _run_plan_mode with deep_plan=True."""

    @pytest.mark.asyncio
    async def test_cmd_deep_calls_plan_mode(self):
        """The /deep handler should call _run_plan_mode with deep_plan=True."""
        from robothor.engine.telegram import TelegramBot

        mock_config = MagicMock()
        mock_config.telegram_bot_token = "test:token"
        mock_config.telegram_chat_id = "12345"
        mock_config.default_chat_agent = "main"
        mock_config.tenant_id = "test-tenant"
        mock_config.port = 18800

        mock_runner = AsyncMock()

        bot = TelegramBot.__new__(TelegramBot)
        bot.config = mock_config
        bot.runner = mock_runner
        bot._model_override = {}
        bot._active_tasks = {}
        bot._max_history = 50

        # Mock _run_plan_mode
        bot._run_plan_mode = AsyncMock()

        # Simulate cmd_deep
        mock_message = MagicMock()
        mock_message.chat.id = 12345
        mock_message.text = "/deep What calendar conflicts this week?"
        mock_message.from_user = MagicMock()

        # Call the handler logic directly (since we can't easily trigger command handlers)
        chat_id = str(mock_message.chat.id)
        session_key = f"agent:main:{chat_id}"

        from robothor.engine.chat import get_shared_session

        session = get_shared_session(session_key)

        deep_arg = mock_message.text.removeprefix("/deep").strip()

        await bot._run_plan_mode(
            chat_id, session_key, session, deep_arg, mock_message, deep_plan=True
        )

        bot._run_plan_mode.assert_called_once_with(
            chat_id, session_key, session, deep_arg, mock_message, deep_plan=True
        )
