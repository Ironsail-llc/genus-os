"""Tool dispatch tests for create_goal / get_goal / update_goal.

These exercise the tool handlers end-to-end — schema registration, dispatch
through the registry, and the new structured-evidence contract.
"""

from __future__ import annotations

from functools import partial
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import _execute_tool as _execute_tool_impl
from robothor.engine.tools.registry import ToolRegistry

_execute_tool = partial(_execute_tool_impl, user_role="service")


def _row(
    *,
    tags: list[str],
    objective: str = "Ship session goal",
    meta: dict[str, Any] | None = None,
    task_id: str = "t1",
):
    return {
        "id": task_id,
        "objective": objective,
        "tags": tags,
        "status": "TODO",
        "session_goal_meta": meta
        or {
            "success_criteria": ["c1"],
            "evidence": [],
            "completion_note": "",
        },
    }


def test_schemas_registered():
    registry = ToolRegistry()
    schemas = set(registry._schemas)
    assert {"create_goal", "get_goal", "update_goal"} <= schemas


def test_update_goal_schema_uses_structured_kind_enum():
    registry = ToolRegistry()
    schema = registry._schemas["update_goal"]["function"]["parameters"]
    kind = schema["properties"]["kind"]
    assert kind["enum"] == ["test_run", "commit", "ci_run", "note"]


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.create_session_goal")
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
async def test_create_goal_creates_when_none_active(mock_get, mock_create):
    mock_get.return_value = None
    mock_create.return_value = "task-new"

    result = await _execute_tool(
        "create_goal",
        {"objective": "Ship session goal feature"},
        agent_id="main",
        tenant_id="default",
    )
    assert "error" not in result
    assert result["goal"]["objective"] == "Ship session goal feature"


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
async def test_create_goal_refuses_duplicate(mock_get):
    mock_get.return_value = _row(tags=["session_goal"])
    result = await _execute_tool(
        "create_goal",
        {"objective": "another"},
        agent_id="main",
        tenant_id="default",
    )
    assert "active goal already exists" in result.get("error", "")


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
async def test_get_goal_returns_active(mock_get):
    mock_get.return_value = _row(tags=["session_goal"], objective="x")
    result = await _execute_tool("get_goal", {}, agent_id="main", tenant_id="default")
    assert result["goal"]["objective"] == "x"


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
async def test_get_goal_none_returns_none(mock_get):
    mock_get.return_value = None
    result = await _execute_tool("get_goal", {}, agent_id="main", tenant_id="default")
    assert result == {"goal": None, "status": "none"}


@pytest.mark.asyncio
async def test_update_goal_rejects_unknown_kind():
    result = await _execute_tool(
        "update_goal",
        {"kind": "implementation", "summary": "did stuff"},
        agent_id="main",
        tenant_id="default",
    )
    assert "kind must be one of" in result.get("error", "")


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
@patch("robothor.engine.session_goal.subprocess.run")
async def test_update_goal_records_valid_commit(mock_run, mock_get, mock_add, tmp_path):
    mock_run.return_value = MagicMock(returncode=0)
    mock_get.return_value = _row(tags=["session_goal"], task_id="t1")
    mock_add.return_value = True

    result = await _execute_tool(
        "update_goal",
        {
            "kind": "commit",
            "summary": "shipped phase 5",
            "reference": "abcdef1234567",
        },
        agent_id="main",
        tenant_id="default",
        workspace=str(tmp_path),
    )
    assert "error" not in result
    kwargs = mock_add.call_args.kwargs
    assert kwargs["valid"] is True
    assert kwargs["kind"] == "commit"


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.complete_session_goal")
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
async def test_update_goal_completion_blocked_without_evidence(mock_get, mock_complete):
    mock_get.return_value = _row(
        tags=["session_goal"],
        meta={"success_criteria": ["c1"], "evidence": [], "completion_note": ""},
    )
    result = await _execute_tool(
        "update_goal",
        {"status": "complete", "completion_note": "shipped"},
        agent_id="main",
        tenant_id="default",
    )
    assert "not ready to complete" in result.get("error", "")
    mock_complete.assert_not_called()


@pytest.mark.asyncio
@patch("robothor.engine.session_goal.dal.complete_session_goal")
@patch("robothor.engine.session_goal.dal.get_active_session_goal")
@patch("robothor.engine.session_goal.subprocess.run")
async def test_update_goal_completes_with_full_evidence(
    mock_run, mock_get, mock_complete, tmp_path
):
    mock_run.return_value = MagicMock(returncode=0)
    mock_get.return_value = _row(
        tags=["session_goal"],
        meta={
            "success_criteria": ["c1"],
            "evidence": [
                {
                    "kind": "test_run",
                    "summary": "ok",
                    "reference": "pytest:passed:5",
                    "recorded_at": "2026-05-09T00:00:00+00:00",
                    "valid": True,
                },
                {
                    "kind": "commit",
                    "summary": "ship",
                    "reference": "abcdef1234567",
                    "recorded_at": "2026-05-09T00:00:00+00:00",
                    "valid": True,
                },
            ],
            "completion_note": "",
        },
    )
    mock_complete.return_value = True

    result = await _execute_tool(
        "update_goal",
        {"status": "complete", "completion_note": "shipped phase 5"},
        agent_id="main",
        tenant_id="default",
        workspace=str(tmp_path),
    )
    assert "error" not in result
    assert result["goal"]["status"] == "complete"
    mock_complete.assert_called_once()
