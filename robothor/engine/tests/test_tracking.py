"""Tests for run tracking DAL."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from robothor.engine.models import AgentRun, RunStatus, RunStep, StepType, TriggerType
from robothor.engine.tracking import (
    MAX_TOOL_OUTPUT_CHARS,
    _truncate_json,
    create_run,
    create_step,
    get_agent_stats,
    get_run,
    list_runs,
    list_schedules,
    list_steps,
    update_run,
    update_schedule_state,
    upsert_schedule,
)


class TestTruncateJson:
    def test_small_data_unchanged(self):
        data = {"key": "value"}
        assert _truncate_json(data) == data

    def test_none_returns_none(self):
        assert _truncate_json(None) is None

    def test_large_data_truncated(self):
        data = {"key": "x" * (MAX_TOOL_OUTPUT_CHARS + 100)}
        result = _truncate_json(data)
        assert result["_truncated"] is True
        assert "preview" in result

    def test_custom_max_chars(self):
        data = {"key": "hello world"}
        result = _truncate_json(data, max_chars=5)
        assert result["_truncated"] is True


class TestCreateRun:
    def test_creates_run(self, mock_db):
        run = AgentRun(
            id=str(uuid.uuid4()),
            agent_id="test-agent",
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        result = create_run(run)
        assert result == run.id
        mock_db["cursor"].execute.assert_called_once()

    def test_creates_run_with_all_fields(self, mock_db):
        run = AgentRun(
            id=str(uuid.uuid4()),
            tenant_id="test-tenant",
            agent_id="email-classifier",
            trigger_type=TriggerType.CRON,
            trigger_detail="0 * * * *",
            correlation_id=str(uuid.uuid4()),
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
            system_prompt_chars=5000,
            user_prompt_chars=200,
            tools_provided=["list_tasks", "create_task"],
            delivery_mode="announce",
        )
        result = create_run(run)
        assert result == run.id


class TestUpdateRun:
    def test_updates_status(self, mock_db):
        result = update_run("run-123", status="completed")
        assert result is True
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "status" in sql

    def test_updates_multiple_fields(self, mock_db):
        result = update_run(
            "run-123",
            status="completed",
            duration_ms=5000,
            model_used="test-model",
            output_text="Done",
        )
        assert result is True
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "duration_ms" in sql
        assert "model_used" in sql
        assert "output_text" in sql

    def test_no_updates_returns_true(self, mock_db):
        result = update_run("run-123")
        assert result is True
        mock_db["cursor"].execute.assert_not_called()


class TestGetRun:
    def test_returns_dict(self, mock_db):
        mock_db["cursor"].fetchone.return_value = {
            "id": "run-1",
            "agent_id": "test",
            "status": "completed",
        }
        result = get_run("run-1")
        assert result is not None
        assert result["id"] == "run-1"

    def test_returns_none_when_not_found(self, mock_db):
        mock_db["cursor"].fetchone.return_value = None
        assert get_run("nonexistent") is None


class TestListRuns:
    def test_lists_with_defaults(self, mock_db):
        mock_db["cursor"].fetchall.return_value = [
            {"id": "r1", "agent_id": "a", "status": "completed"},
        ]
        results = list_runs()
        assert len(results) == 1

    def test_filters_by_agent(self, mock_db):
        list_runs(agent_id="test-agent")
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "agent_id = %s" in sql

    def test_filters_by_status(self, mock_db):
        list_runs(status="failed")
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "status = %s" in sql


class TestCreateStep:
    def test_creates_step(self, mock_db):
        step = RunStep(
            run_id="run-1",
            step_number=1,
            step_type=StepType.LLM_CALL,
            model="test-model",
            input_tokens=100,
            output_tokens=50,
        )
        result = create_step(step)
        assert result == step.id

    def test_creates_tool_call_step(self, mock_db):
        step = RunStep(
            run_id="run-1",
            step_number=2,
            step_type=StepType.TOOL_CALL,
            tool_name="list_tasks",
            tool_input={"status": "TODO"},
            tool_output={"tasks": [], "count": 0},
        )
        result = create_step(step)
        assert result == step.id

    def test_truncates_large_output(self, mock_db):
        step = RunStep(
            run_id="run-1",
            step_number=3,
            step_type=StepType.TOOL_CALL,
            tool_name="search_records",
            tool_output={"data": "x" * 10000},
        )
        create_step(step)
        # The SQL params should have the truncated output
        call_args = mock_db["cursor"].execute.call_args[0][1]
        output_json = call_args[6]  # tool_output is at index 6
        parsed = json.loads(output_json)
        assert parsed.get("_truncated") is True


class TestListSteps:
    def test_lists_ordered(self, mock_db):
        mock_db["cursor"].fetchall.return_value = [
            {"step_number": 1, "step_type": "llm_call"},
            {"step_number": 2, "step_type": "tool_call"},
        ]
        results = list_steps("run-1")
        assert len(results) == 2
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "ORDER BY step_number" in sql


class TestSchedules:
    def test_upsert_schedule(self, mock_db):
        result = upsert_schedule(
            agent_id="test-agent",
            cron_expr="0 * * * *",
            timeout_seconds=600,
            model_primary="test-model",
        )
        assert result is True
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "ON CONFLICT" in sql

    def test_update_schedule_state(self, mock_db):
        result = update_schedule_state(
            agent_id="test-agent",
            last_status="completed",
            last_duration_ms=5000,
            consecutive_errors=0,
        )
        assert result is True

    def test_list_schedules(self, mock_db):
        mock_db["cursor"].fetchall.return_value = [
            {"agent_id": "a1", "enabled": True},
        ]
        results = list_schedules()
        assert len(results) == 1

    def test_list_schedules_enabled_only(self, mock_db):
        list_schedules(enabled_only=True)
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "enabled = TRUE" in sql


class TestAgentStats:
    def test_returns_stats(self, mock_db):
        mock_db["cursor"].fetchone.return_value = {
            "total_runs": 10,
            "completed": 8,
            "failed": 2,
            "timeouts": 0,
            "avg_duration_ms": 3000,
            "total_input_tokens": 50000,
            "total_output_tokens": 10000,
            "total_cost_usd": 0.05,
        }
        stats = get_agent_stats("test-agent")
        assert stats["total_runs"] == 10
        assert stats["completed"] == 8
