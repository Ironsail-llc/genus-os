"""Tests for AgentSession — per-run state management."""

from __future__ import annotations

from robothor.engine.models import RunStatus, StepType, TriggerType
from robothor.engine.session import AgentSession


class TestSessionLifecycle:
    def test_init(self):
        session = AgentSession("test-agent")
        assert session.run.agent_id == "test-agent"
        assert session.run.status == RunStatus.PENDING
        assert session.messages == []

    def test_start(self):
        session = AgentSession("test-agent")
        session.start("System prompt", "User message", ["list_tasks"], "announce")
        assert session.run.status == RunStatus.RUNNING
        assert session.run.started_at is not None
        assert session.run.system_prompt_chars == len("System prompt")
        assert session.run.user_prompt_chars == len("User message")
        assert len(session.messages) == 2
        assert session.messages[0]["role"] == "system"
        assert session.messages[1]["role"] == "user"

    def test_start_with_conversation_history(self):
        session = AgentSession("test-agent")
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        session.start(
            "System prompt", "Follow-up", ["list_tasks"], "announce", conversation_history=history
        )
        assert len(session.messages) == 4  # system + 2 history + user
        assert session.messages[0]["role"] == "system"
        assert session.messages[1]["role"] == "user"
        assert session.messages[1]["content"] == "Hello"
        assert session.messages[2]["role"] == "assistant"
        assert session.messages[2]["content"] == "Hi there!"
        assert session.messages[3]["role"] == "user"
        assert session.messages[3]["content"] == "Follow-up"

    def test_start_with_empty_history(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [], conversation_history=[])
        assert len(session.messages) == 2  # system + user (empty list = no extras)

    def test_start_with_none_history(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [], conversation_history=None)
        assert len(session.messages) == 2  # system + user

    def test_complete(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        run = session.complete("Output text")
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Output text"
        assert run.completed_at is not None
        assert run.duration_ms is not None
        assert run.duration_ms >= 0

    def test_fail(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        run = session.fail("Something went wrong", "traceback here")
        assert run.status == RunStatus.FAILED
        assert run.error_message == "Something went wrong"
        assert run.error_traceback == "traceback here"

    def test_timeout(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        run = session.timeout()
        assert run.status == RunStatus.TIMEOUT


class TestStepRecording:
    def test_record_llm_call(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        step = session.record_llm_call(
            model="test-model",
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
            assistant_message={"role": "assistant", "content": "Hello"},
        )
        assert step.step_type == StepType.LLM_CALL
        assert step.model == "test-model"
        assert session.run.input_tokens == 100
        assert session.run.output_tokens == 50
        assert "test-model" in session.run.models_attempted
        assert len(session.messages) == 3  # sys + user + assistant
        assert len(session.run.steps) == 1

    def test_record_tool_call(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        step = session.record_tool_call(
            tool_name="list_tasks",
            tool_input={"status": "TODO"},
            tool_output={"tasks": [], "count": 0},
            tool_call_id="call_1",
            duration_ms=100,
        )
        assert step.step_type == StepType.TOOL_CALL
        assert step.tool_name == "list_tasks"
        assert len(session.messages) == 3  # sys + user + tool result
        assert session.messages[2]["role"] == "tool"
        assert session.messages[2]["tool_call_id"] == "call_1"

    def test_record_error(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        step = session.record_error("Bad things happened")
        assert step.step_type == StepType.ERROR
        assert step.error_message == "Bad things happened"

    def test_step_numbers_increment(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        s1 = session.record_llm_call("m", assistant_message={"role": "assistant", "content": "hi"})
        s2 = session.record_tool_call("t", {}, {}, "c1")
        s3 = session.record_llm_call(
            "m", assistant_message={"role": "assistant", "content": "done"}
        )
        assert s1.step_number == 1
        assert s2.step_number == 2
        assert s3.step_number == 3

    def test_token_accumulation(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        session.record_llm_call(
            "m",
            input_tokens=100,
            output_tokens=50,
            assistant_message={"role": "assistant", "content": "1"},
        )
        session.record_llm_call(
            "m",
            input_tokens=200,
            output_tokens=80,
            assistant_message={"role": "assistant", "content": "2"},
        )
        assert session.run.input_tokens == 300
        assert session.run.output_tokens == 130


class TestGetFinalText:
    def test_extracts_last_assistant_message(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        session.record_llm_call("m", assistant_message={"role": "assistant", "content": "first"})
        session.record_tool_call("t", {}, {}, "c1")
        session.record_llm_call("m", assistant_message={"role": "assistant", "content": "final"})
        assert session.get_final_text() == "final"

    def test_returns_none_when_no_assistant(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        assert session.get_final_text() is None

    def test_skips_tool_call_only_messages(self):
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        # Assistant message with tool_calls but no content
        session.messages.append({"role": "assistant", "tool_calls": [{}]})
        assert session.get_final_text() is None


class TestContextPreservation:
    def test_trigger_info_preserved(self):
        session = AgentSession(
            "test-agent",
            trigger_type=TriggerType.CRON,
            trigger_detail="0 * * * *",
            tenant_id="custom-tenant",
            correlation_id="corr-123",
        )
        assert session.run.trigger_type == TriggerType.CRON
        assert session.run.trigger_detail == "0 * * * *"
        assert session.run.tenant_id == "custom-tenant"
        assert session.run.correlation_id == "corr-123"
