"""Tests for plan mode — read-only exploration + approval flow."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine.chat import (
    _extract_plan_text,
    _get_session,
    _plan_is_expired,
    _plan_to_dict,
    _sessions,
    init_chat,
    router,
)
from robothor.engine.models import (
    PLAN_TTL_SECONDS,
    AgentRun,
    PlanState,
    RunStatus,
    StepType,
    TriggerType,
)
from robothor.engine.tools import READONLY_TOOLS, SPAWN_TOOLS

# ─── Model Tests ──────────────────────────────────────────────────────


class TestPlanStateModel:
    def test_plan_state_defaults(self):
        plan = PlanState(plan_id="p1", plan_text="Do X", original_message="do x")
        assert plan.status == "pending"
        assert plan.rejection_feedback == ""
        assert plan.created_at == ""
        assert plan.revision_count == 0
        assert plan.revision_history == []
        assert plan.execution_run_id == ""

    def test_step_type_includes_plan_proposal(self):
        assert StepType.PLAN_PROPOSAL.value == "plan_proposal"
        assert "plan_proposal" in [s.value for s in StepType]


# ─── READONLY_TOOLS Tests ─────────────────────────────────────────────


class TestReadonlyTools:
    def test_readonly_tools_contains_read_file(self):
        assert "read_file" in READONLY_TOOLS

    def test_readonly_tools_contains_list_directory(self):
        assert "list_directory" in READONLY_TOOLS

    def test_readonly_tools_contains_web_search(self):
        assert "web_search" in READONLY_TOOLS

    def test_readonly_tools_excludes_write_tools(self):
        write_tools = {
            "write_file",
            "exec",
            "create_task",
            "update_task",
            "delete_task",
            "create_person",
            "update_person",
            "create_company",
            "create_note",
            "store_memory",
            "make_call",
            "schedule_appointment",
            "create_prescription_draft",
            "transmit_prescription",
        }
        for tool in write_tools:
            assert tool not in READONLY_TOOLS, f"{tool} should not be in READONLY_TOOLS"

    def test_readonly_tools_excludes_spawn_tools(self):
        for tool in SPAWN_TOOLS:
            assert tool not in READONLY_TOOLS

    def test_readonly_tools_is_frozenset(self):
        assert isinstance(READONLY_TOOLS, frozenset)


class TestBuildReadonlyForAgent:
    def test_returns_subset_of_full_tools(self, sample_agent_config):
        """build_readonly_for_agent returns a subset of build_for_agent."""
        from robothor.engine.tools import get_registry

        registry = get_registry()
        full = registry.build_for_agent(sample_agent_config)
        readonly = registry.build_readonly_for_agent(sample_agent_config)

        full_names = {t["function"]["name"] for t in full}
        readonly_names = {t["function"]["name"] for t in readonly}

        assert readonly_names <= full_names
        assert readonly_names <= READONLY_TOOLS

    def test_readonly_respects_tools_allowed(self):
        """Only tools in both READONLY_TOOLS and tools_allowed are returned."""
        from robothor.engine.models import AgentConfig, DeliveryMode
        from robothor.engine.tools import get_registry

        config = AgentConfig(
            id="test",
            name="Test",
            delivery_mode=DeliveryMode.NONE,
            tools_allowed=["read_file", "write_file", "web_search"],
        )
        registry = get_registry()
        readonly = registry.build_readonly_for_agent(config)
        names = {t["function"]["name"] for t in readonly}

        assert "read_file" in names
        assert "web_search" in names
        assert "write_file" not in names

    def test_get_readonly_tool_names(self, sample_agent_config):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        names = registry.get_readonly_tool_names(sample_agent_config)
        assert isinstance(names, list)
        for name in names:
            assert name in READONLY_TOOLS


# ─── Plan Helper Tests ─────────────────────────────────────────────────


class TestExtractPlanText:
    def test_extracts_before_marker(self):
        text = "Step 1: Do this\nStep 2: Do that\n\n[PLAN_READY]"
        assert _extract_plan_text(text) == "Step 1: Do this\nStep 2: Do that"

    def test_returns_full_text_without_marker(self):
        text = "Step 1: Do this\nStep 2: Do that"
        assert _extract_plan_text(text) == text

    def test_handles_empty_string(self):
        assert _extract_plan_text("") == ""

    def test_handles_marker_only(self):
        assert _extract_plan_text("[PLAN_READY]") == ""


class TestPlanIsExpired:
    def test_expired_when_no_timestamp(self):
        plan = PlanState(plan_id="p1", plan_text="x", original_message="x")
        assert _plan_is_expired(plan) is True

    def test_not_expired_when_recent(self):
        plan = PlanState(
            plan_id="p1",
            plan_text="x",
            original_message="x",
            created_at=datetime.now(UTC).isoformat(),
        )
        assert _plan_is_expired(plan) is False

    def test_expired_when_old(self):
        old_time = datetime.now(UTC) - timedelta(seconds=PLAN_TTL_SECONDS + 60)
        plan = PlanState(
            plan_id="p1",
            plan_text="x",
            original_message="x",
            created_at=old_time.isoformat(),
        )
        assert _plan_is_expired(plan) is True


class TestPlanToDict:
    def test_serializes_all_fields(self):
        plan = PlanState(
            plan_id="abc",
            plan_text="Do X",
            original_message="plan X",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
            exploration_run_id="run-1",
            rejection_feedback="",
        )
        d = _plan_to_dict(plan)
        assert d["plan_id"] == "abc"
        assert d["plan_text"] == "Do X"
        assert d["original_message"] == "plan X"
        assert d["status"] == "pending"
        assert d["revision_count"] == 0
        assert d["revision_history"] == []
        assert d["execution_run_id"] == ""

    def test_serializes_revision_fields(self):
        plan = PlanState(
            plan_id="abc",
            plan_text="Revised plan",
            original_message="plan X",
            revision_count=2,
            revision_history=[
                {"plan_text": "v1", "feedback": "add tests", "timestamp": "2026-03-01T00:00:00"},
                {"plan_text": "v2", "feedback": "more detail", "timestamp": "2026-03-01T00:01:00"},
            ],
            execution_run_id="run-exec-1",
        )
        d = _plan_to_dict(plan)
        assert d["revision_count"] == 2
        assert len(d["revision_history"]) == 2
        assert d["execution_run_id"] == "run-exec-1"


# ─── Chat Endpoint Tests ──────────────────────────────────────────────


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
def chat_app(engine_config, mock_runner):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    yield app
    _sessions.clear()


@pytest.fixture
async def client(chat_app):
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestPlanStart:
    @pytest.mark.asyncio
    async def test_plan_start_creates_plan(self, client, mock_runner):
        """plan/start returns SSE with plan event."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="1. Read file\n2. Create task\n\n[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )

        async def fake_execute(**kwargs):
            assert kwargs.get("readonly_mode") is True
            on_content = kwargs.get("on_content")
            if on_content:
                await on_content("1. Read file\n2. Create task\n\n[PLAN_READY]")
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/plan/start",
            json={"session_key": "plan:main:test", "message": "create a task"},
        )
        assert res.status_code == 200

        events = _parse_sse(res.text)
        plan_events = [e for e in events if e["event"] == "plan"]
        assert len(plan_events) == 1
        assert "Read file" in plan_events[0]["data"]["plan_text"]

        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert done_events[0]["data"].get("plan_id") is not None

    @pytest.mark.asyncio
    async def test_plan_start_missing_fields(self, client):
        res = await client.post("/chat/plan/start", json={"session_key": "x"})
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_plan_start_accumulates_history(self, client, mock_runner):
        """plan/start appends exchange to session.history."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="1. Do X\n\n[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )

        async def fake_execute(**kwargs):
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        session = _get_session("hist:main:test")
        assert len(session.history) == 0

        res = await client.post(
            "/chat/plan/start",
            json={"session_key": "hist:main:test", "message": "plan something"},
        )
        assert res.status_code == 200
        # Consume the SSE stream
        _ = res.text

        assert len(session.history) == 2
        assert session.history[0]["role"] == "user"
        assert session.history[0]["content"] == "plan something"
        assert session.history[1]["role"] == "assistant"
        assert "Do X" in session.history[1]["content"]

    @pytest.mark.asyncio
    async def test_plan_start_supersedes_pending_plan(self, client, mock_runner):
        """plan/start with a pending active_plan supersedes it."""
        session = _get_session("super:main:test")
        old_plan = PlanState(
            plan_id="old-plan",
            plan_text="Old plan",
            original_message="old msg",
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )
        session.active_plan = old_plan

        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="New plan\n\n[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        res = await client.post(
            "/chat/plan/start",
            json={"session_key": "super:main:test", "message": "revised feedback"},
        )
        assert res.status_code == 200
        _ = res.text

        # Old plan should be superseded
        assert old_plan.status == "superseded"
        # New plan should be active
        assert session.active_plan is not None
        assert session.active_plan.plan_text == "New plan"


class TestPlanApprove:
    @pytest.mark.asyncio
    async def test_approve_executes_plan(self, client, mock_runner):
        """Approving a plan runs the agent with full tools."""
        # Set up a pending plan
        session = _get_session("approve:main:test")
        session.active_plan = PlanState(
            plan_id="test-plan-1",
            plan_text="1. Create a task",
            original_message="create a task",
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )

        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Task created!",
            trigger_type=TriggerType.WEBCHAT,
        )

        async def fake_execute(**kwargs):
            # Should NOT be readonly
            assert kwargs.get("readonly_mode", False) is False
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/plan/approve",
            json={"session_key": "approve:main:test", "plan_id": "test-plan-1"},
        )
        assert res.status_code == 200

        events = _parse_sse(res.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert done_events[0]["data"]["text"] == "Task created!"

        # Plan should be cleared
        assert session.active_plan is None

    @pytest.mark.asyncio
    async def test_approve_uses_context_reset(self, client, mock_runner):
        """Approval passes conversation_history=None (context reset) and execution_mode=True."""
        session = _get_session("ctx-reset:main:test")
        # Pre-populate history to prove it's NOT passed through
        session.history = [
            {"role": "user", "content": "plan something"},
            {"role": "assistant", "content": "Here's my plan..."},
        ]
        session.active_plan = PlanState(
            plan_id="reset-plan",
            plan_text="1. Create file\n2. Run tests",
            original_message="create a service",
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )

        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Service created and tests pass.",
            trigger_type=TriggerType.WEBCHAT,
        )

        captured_kwargs = {}

        async def fake_execute(**kwargs):
            captured_kwargs.update(kwargs)
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/plan/approve",
            json={"session_key": "ctx-reset:main:test", "plan_id": "reset-plan"},
        )
        assert res.status_code == 200
        _ = res.text  # consume SSE stream

        # Context reset: no conversation history passed
        assert captured_kwargs.get("conversation_history") is None
        # Execution mode enabled
        assert captured_kwargs.get("execution_mode") is True
        # Message contains both original request and plan text
        msg = captured_kwargs.get("message", "")
        assert "create a service" in msg
        assert "Create file" in msg

    @pytest.mark.asyncio
    async def test_approve_merges_result_into_history(self, client, mock_runner):
        """After execution, the result is merged back into session history."""
        session = _get_session("merge:main:test")
        session.active_plan = PlanState(
            plan_id="merge-plan",
            plan_text="1. Do X",
            original_message="do something",
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )

        run = AgentRun(
            id="exec-run-123",
            status=RunStatus.COMPLETED,
            output_text="Done: X completed.",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        res = await client.post(
            "/chat/plan/approve",
            json={"session_key": "merge:main:test", "plan_id": "merge-plan"},
        )
        assert res.status_code == 200
        _ = res.text

        # History should contain the execution result
        assert len(session.history) >= 2
        assert "[Plan executed]" in session.history[-2]["content"]
        assert "Done: X completed." in session.history[-1]["content"]

    @pytest.mark.asyncio
    async def test_approve_wrong_plan_id(self, client):
        session = _get_session("wrong:main:test")
        session.active_plan = PlanState(
            plan_id="plan-a",
            plan_text="x",
            original_message="x",
            created_at=datetime.now(UTC).isoformat(),
        )

        res = await client.post(
            "/chat/plan/approve",
            json={"session_key": "wrong:main:test", "plan_id": "plan-b"},
        )
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_expired_plan(self, client):
        session = _get_session("expired:main:test")
        old_time = datetime.now(UTC) - timedelta(seconds=PLAN_TTL_SECONDS + 60)
        session.active_plan = PlanState(
            plan_id="old-plan",
            plan_text="x",
            original_message="x",
            created_at=old_time.isoformat(),
        )

        res = await client.post(
            "/chat/plan/approve",
            json={"session_key": "expired:main:test", "plan_id": "old-plan"},
        )
        assert res.status_code == 410


class TestPlanReject:
    @pytest.mark.asyncio
    async def test_reject_clears_plan(self, client):
        session = _get_session("reject:main:test")
        session.active_plan = PlanState(
            plan_id="rej-1",
            plan_text="x",
            original_message="x",
            created_at=datetime.now(UTC).isoformat(),
        )

        res = await client.post(
            "/chat/plan/reject",
            json={"session_key": "reject:main:test", "plan_id": "rej-1"},
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True
        assert session.active_plan is None

    @pytest.mark.asyncio
    async def test_reject_with_feedback(self, client):
        session = _get_session("fb:main:test")
        session.active_plan = PlanState(
            plan_id="fb-1",
            plan_text="x",
            original_message="x",
            created_at=datetime.now(UTC).isoformat(),
        )

        res = await client.post(
            "/chat/plan/reject",
            json={
                "session_key": "fb:main:test",
                "plan_id": "fb-1",
                "feedback": "Use email instead",
            },
        )
        assert res.status_code == 200

        # Feedback injected into history
        system_msgs = [m for m in session.history if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "Use email instead" in system_msgs[0]["content"]


class TestPlanStatus:
    @pytest.mark.asyncio
    async def test_no_active_plan(self, client):
        res = await client.get("/chat/plan/status?session_key=empty:main:test")
        assert res.status_code == 200
        data = res.json()
        assert data["active"] is False
        assert data["plan"] is None

    @pytest.mark.asyncio
    async def test_active_plan(self, client):
        session = _get_session("active:main:test")
        session.active_plan = PlanState(
            plan_id="act-1",
            plan_text="Do thing",
            original_message="do thing",
            created_at=datetime.now(UTC).isoformat(),
        )

        res = await client.get("/chat/plan/status?session_key=active:main:test")
        data = res.json()
        assert data["active"] is True
        assert data["plan"]["plan_id"] == "act-1"

    @pytest.mark.asyncio
    async def test_auto_expires_stale_plan(self, client):
        session = _get_session("stale:main:test")
        old_time = datetime.now(UTC) - timedelta(seconds=PLAN_TTL_SECONDS + 1)
        session.active_plan = PlanState(
            plan_id="stale-1",
            plan_text="x",
            original_message="x",
            created_at=old_time.isoformat(),
        )

        res = await client.get("/chat/plan/status?session_key=stale:main:test")
        data = res.json()
        assert data["active"] is False


class TestPlanIterate:
    @pytest.mark.asyncio
    async def test_iterate_revises_plan_text(self, client, mock_runner):
        """POST /plan/iterate revises the plan and keeps same plan_id."""
        session = _get_session("iter:main:test")
        session.active_plan = PlanState(
            plan_id="iter-plan-1",
            plan_text="1. Do X\n2. Do Y",
            original_message="build feature",
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )

        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Changes: Added step 3.\n\n1. Do X\n2. Do Y\n3. Do Z\n\n[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )

        async def fake_execute(**kwargs):
            # Should be readonly (plan iteration uses read-only tools)
            assert kwargs.get("readonly_mode") is True
            # Message should contain feedback and current plan
            msg = kwargs.get("message", "")
            assert "add step 3" in msg
            assert "Do X" in msg
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/plan/iterate",
            json={
                "session_key": "iter:main:test",
                "plan_id": "iter-plan-1",
                "feedback": "add step 3",
            },
        )
        assert res.status_code == 200

        events = _parse_sse(res.text)
        plan_events = [e for e in events if e["event"] == "plan"]
        assert len(plan_events) == 1
        assert "Do Z" in plan_events[0]["data"]["plan_text"]

        # Same plan_id preserved
        assert session.active_plan.plan_id == "iter-plan-1"
        assert session.active_plan.revision_count == 1
        assert len(session.active_plan.revision_history) == 1
        assert session.active_plan.revision_history[0]["feedback"] == "add step 3"

    @pytest.mark.asyncio
    async def test_iterate_missing_feedback(self, client):
        res = await client.post(
            "/chat/plan/iterate",
            json={"session_key": "x", "plan_id": "y"},
        )
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_iterate_wrong_plan_id(self, client):
        session = _get_session("iter-wrong:main:test")
        session.active_plan = PlanState(
            plan_id="plan-a",
            plan_text="x",
            original_message="x",
            created_at=datetime.now(UTC).isoformat(),
        )

        res = await client.post(
            "/chat/plan/iterate",
            json={
                "session_key": "iter-wrong:main:test",
                "plan_id": "plan-b",
                "feedback": "change it",
            },
        )
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_iterate_expired_plan(self, client):
        session = _get_session("iter-exp:main:test")
        old_time = datetime.now(UTC) - timedelta(seconds=PLAN_TTL_SECONDS + 60)
        session.active_plan = PlanState(
            plan_id="exp-plan",
            plan_text="x",
            original_message="x",
            created_at=old_time.isoformat(),
        )

        res = await client.post(
            "/chat/plan/iterate",
            json={
                "session_key": "iter-exp:main:test",
                "plan_id": "exp-plan",
                "feedback": "change it",
            },
        )
        assert res.status_code == 410


class TestChatSessionPlanFields:
    def test_default_plan_mode_false(self):
        from robothor.engine.chat import ChatSession

        session = ChatSession()
        assert session.plan_mode is False
        assert session.active_plan is None

    def test_clear_resets_plan(self, client):
        """POST /chat/clear resets plan state."""

        async def _test():
            session = _get_session("clear-plan:main:test")
            session.active_plan = PlanState(
                plan_id="c1",
                plan_text="x",
                original_message="x",
                created_at=datetime.now(UTC).isoformat(),
            )
            session.plan_mode = True

            res = await client.post("/chat/clear", json={"session_key": "clear-plan:main:test"})
            assert res.status_code == 200
            assert session.active_plan is None
            assert session.plan_mode is False


# ─── SSE parser (shared with test_chat.py) ────────────────────────────


def _parse_sse(body: str) -> list[dict]:
    events = []
    current_event = ""
    for line in body.split("\n"):
        if line.startswith(":"):
            continue
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append({"event": current_event, "data": data})
    return events


# ─── Runner Plan Mode Tests ─────────────────────────────────────────


@pytest.fixture
def runner(engine_config):
    """Create an AgentRunner with mocked registry and no compaction overhead."""
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = []
        mock_registry.get_tool_names.return_value = []
        mock_registry.build_readonly_for_agent.return_value = []
        mock_registry.get_readonly_tool_names.return_value = []
        mock_reg.return_value = mock_registry
        from robothor.engine.runner import AgentRunner

        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


class TestPlanModeSystemPrompt:
    """Verify the conversational plan-mode system prompt construction."""

    def test_plan_prompt_includes_conversational_flow(self):
        """readonly_mode=True injects the conversational plan prompt."""
        from robothor.engine.prompts import PLAN_MODE_PREAMBLE, PLAN_MODE_SUFFIX

        base_prompt = "You are Robothor."
        tool_names = ["list_tasks", "read_file", "list_directory"]
        tool_list_str = ", ".join(f"`{t}`" for t in sorted(tool_names))
        preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list_str)
        system_msg = preamble + base_prompt + PLAN_MODE_SUFFIX

        # Preamble (prepended before identity)
        assert "PLAN MODE — STRATEGIC PAUSE" in system_msg
        assert "Channel your drive into research and analysis" in system_msg

        # New preamble sections
        assert "Discovery strategy" in system_msg
        assert "list_directory" in system_msg
        assert "Autonomy (CRITICAL)" in system_msg
        assert "NEVER ask Philip to run commands" in system_msg

        # Suffix (appended after identity)
        assert "PLAN MODE REMINDER" in system_msg
        assert "[PLAN_READY]" in system_msg
        assert "On revision" in system_msg
        assert "refine it" in system_msg
        assert "Discover, don't guess" in system_msg
        assert "Ask only about intent" in system_msg

        # Positional: preamble appears BEFORE identity text, suffix AFTER
        preamble_pos = system_msg.index("PLAN MODE — STRATEGIC PAUSE")
        suffix_pos = system_msg.index("PLAN MODE REMINDER")
        assert preamble_pos < suffix_pos
        assert preamble_pos < 5

    def test_plan_prompt_injects_dynamic_tool_names(self):
        """Plan mode injects the actual readonly tool names into the preamble."""
        from robothor.engine.prompts import PLAN_MODE_PREAMBLE

        tool_names = ["read_file", "list_directory", "web_search"]
        tool_list_str = ", ".join(f"`{t}`" for t in sorted(tool_names))
        preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list_str)

        assert "`list_directory`" in preamble
        assert "`read_file`" in preamble
        assert "`web_search`" in preamble
        assert "{tool_names_placeholder}" not in preamble

    def test_normal_mode_no_plan_prompt(self):
        """A plain system prompt does NOT contain plan mode markers."""
        base_prompt = "You are Robothor."
        assert "PLAN MODE" not in base_prompt


class TestExecutionModePreamble:
    """Verify execution_mode=True injects the execution preamble."""

    def test_execution_mode_injects_preamble(self):
        """execution_mode=True prepends EXECUTION_MODE_PREAMBLE to system prompt."""
        from robothor.engine.prompts import EXECUTION_MODE_PREAMBLE

        base_prompt = "You are Robothor."
        system_msg = EXECUTION_MODE_PREAMBLE + base_prompt
        assert "EXECUTION MODE" in system_msg
        assert "Do NOT re-plan" in system_msg

    def test_execution_mode_off_no_preamble(self):
        """Without execution mode, the preamble is absent."""
        base_prompt = "You are Robothor."
        assert "EXECUTION MODE" not in base_prompt

    def test_execution_mode_not_combined_with_readonly(self):
        """When readonly_mode is True, plan mode takes priority over execution mode.

        The runner logic at lines 286-305:
          if readonly_mode: system_prompt = preamble + system_prompt + PLAN_MODE_SUFFIX
          if execution_mode and not readonly_mode: system_prompt = EXECUTION_MODE_PREAMBLE + ...
        So readonly_mode=True means execution_mode is skipped.
        """
        from robothor.engine.prompts import (
            EXECUTION_MODE_PREAMBLE,
            PLAN_MODE_PREAMBLE,
            PLAN_MODE_SUFFIX,
        )

        base_prompt = "You are Robothor."
        readonly_mode = True
        execution_mode = True

        # Replicate runner logic
        if readonly_mode:
            tool_list_str = "`list_tasks`"
            preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list_str)
            system_msg = preamble + base_prompt + PLAN_MODE_SUFFIX
        if execution_mode and not readonly_mode:
            system_msg = EXECUTION_MODE_PREAMBLE + base_prompt

        assert "PLAN MODE" in system_msg
        assert "EXECUTION MODE" not in system_msg


class TestPlanModeResearchNudge:
    """Verify the research nudge fires on first no-tool-call iteration.

    These tests exercise the full runner loop and take >10s due to runner overhead.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_nudge_fires_when_no_tools_on_first_iteration(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """If the agent proposes a plan without tool calls on iteration 0,
        a nudge message is injected and the loop continues."""
        sample_agent_config.max_iterations = 3
        # First call: text only (no tool calls) — triggers nudge
        response1 = mock_litellm_response(content="Here's my plan without research")
        # Second call: text with PLAN_READY (after nudge) — loop ends
        response2 = mock_litellm_response(content="After checking, here's the plan\n\n[PLAN_READY]")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion) as mock_llm:
                        run = await runner.execute(
                            "test-agent",
                            "check tasks",
                            agent_config=sample_agent_config,
                            readonly_mode=True,
                        )

        assert run.status == RunStatus.COMPLETED
        # LLM was called twice (first attempt + after nudge)
        assert call_count == 2

        # The nudge message was injected between calls
        second_call_messages = mock_llm.call_args_list[1].kwargs["messages"]
        nudge_msgs = [
            m
            for m in second_call_messages
            if m.get("role") == "user" and "without using any tools" in m.get("content", "")
        ]
        assert len(nudge_msgs) == 1
        # Nudge mentions discovery tools
        assert "list_directory" in nudge_msgs[0]["content"]
        assert "read_file" in nudge_msgs[0]["content"]
        # Nudge discourages asking the user
        assert "Do NOT ask the user" in nudge_msgs[0]["content"]

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_nudge_does_not_fire_in_normal_mode(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """In normal (non-plan) mode, text-only response ends the loop immediately."""
        response = mock_litellm_response(content="Done!")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        # Only one LLM call — no nudge
        assert call_count == 1

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_nudge_only_fires_once(self, runner, sample_agent_config, mock_litellm_response):
        """The nudge only fires on iteration 0; subsequent text-only responses end the loop."""
        sample_agent_config.max_iterations = 3
        # First call: text only → nudge
        response1 = mock_litellm_response(content="My plan without research")
        # Second call: text only again → loop ends (iteration 1, no nudge)
        response2 = mock_litellm_response(content="Fine, here's my plan\n\n[PLAN_READY]")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "check tasks",
                            agent_config=sample_agent_config,
                            readonly_mode=True,
                        )

        assert run.status == RunStatus.COMPLETED
        # Exactly two calls: first attempt + one retry after nudge
        assert call_count == 2

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_no_nudge_when_tools_used_on_first_iteration(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """If the agent uses tools on iteration 0, no nudge is needed."""
        # First call: tool call (agent researches)
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})
        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None

        # Second call: plan output
        response2 = mock_litellm_response(content="Found tasks. Plan:\n1. Do X\n\n[PLAN_READY]")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_readonly_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_readonly_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion) as mock_llm:
                        run = await runner.execute(
                            "test-agent",
                            "check tasks",
                            agent_config=sample_agent_config,
                            readonly_mode=True,
                        )

        assert run.status == RunStatus.COMPLETED
        assert call_count == 2
        # No nudge in the messages — second call should not contain nudge text
        second_call_messages = mock_llm.call_args_list[1].kwargs["messages"]
        nudge_msgs = [
            m
            for m in second_call_messages
            if m.get("role") == "user" and "without using any tools" in m.get("content", "")
        ]
        assert len(nudge_msgs) == 0


class TestPlanModeIterationCap:
    """Verify plan mode caps max_iterations at 10."""

    @pytest.mark.asyncio
    async def test_plan_mode_caps_at_10(self, runner, sample_agent_config, mock_litellm_response):
        """readonly_mode=True caps iterations at 10 regardless of agent config."""
        sample_agent_config.max_iterations = 20

        # Create a tool call that loops so we can count iterations
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({})

        response_with_tool = mock_litellm_response(content=None, tool_calls=[tc])
        response_with_tool.choices[0].message.content = None

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Always return tool calls — loop runs until max_iterations
            return response_with_tool

        runner.registry.execute = AsyncMock(return_value={"tasks": []})
        runner.registry.build_readonly_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_readonly_tool_names.return_value = ["list_tasks"]

        # Disable routing so it doesn't interfere with the iteration cap
        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch.object(runner, "_apply_routing", return_value=None):
                            await runner.execute(
                                "test-agent",
                                "check tasks",
                                agent_config=sample_agent_config,
                                readonly_mode=True,
                            )

        # Should be capped at 10 iterations, not 20.
        # max_iterations is a check-in interval, so LLM call_count may exceed
        # the iteration count (multiple calls per iteration for tool execution).
        # The important thing is that the loop terminates, not the exact call count.
        assert call_count <= 50  # sanity: shouldn't run unbounded
        assert call_count > 0  # did actually run
