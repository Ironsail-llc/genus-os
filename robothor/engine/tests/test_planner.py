"""Tests for the planning phase."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.planner import PlanResult, format_plan_context, generate_plan


@pytest.mark.asyncio
async def test_generate_plan_success():
    """Plan generation returns structured PlanResult on success."""
    plan_data = {
        "difficulty": "moderate",
        "estimated_steps": 3,
        "plan": [
            {"step": 1, "action": "Read inbox", "tool": "read_file"},
            {"step": 2, "action": "Classify emails", "tool": "exec"},
            {"step": 3, "action": "Create tasks", "tool": "create_task"},
        ],
        "risks": ["Inbox file may not exist"],
        "success_criteria": "All emails classified and tasks created",
    }

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(plan_data)

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Classify inbox", ["read_file", "exec", "create_task"], "test-model")

    assert result.success is True
    assert result.difficulty == "moderate"
    assert result.estimated_steps == 3
    assert len(result.plan) == 3
    assert result.risks == ["Inbox file may not exist"]


@pytest.mark.asyncio
async def test_generate_plan_all_models_fail():
    """Returns failed PlanResult when all models fail."""
    with patch("litellm.acompletion", side_effect=Exception("API error")):
        result = await generate_plan("Do stuff", ["tool1"], "bad-model")

    assert result.success is False
    assert "failed" in result.error.lower()


@pytest.mark.asyncio
async def test_generate_plan_invalid_json():
    """Returns failed PlanResult when LLM returns invalid JSON."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "not valid json"

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Do stuff", ["tool1"], "test-model")

    assert result.success is False


def test_format_plan_context_with_plan():
    """format_plan_context produces readable output."""
    plan = PlanResult(
        success=True,
        difficulty="complex",
        plan=[
            {"step": 1, "action": "Read file", "tool": "read_file"},
            {"step": 2, "action": "Analyze"},
        ],
        risks=["File may be large"],
        success_criteria="Analysis complete",
    )
    text = format_plan_context(plan)
    assert "[EXECUTION PLAN]" in text
    assert "complex" in text
    assert "Read file" in text
    assert "read_file" in text
    assert "File may be large" in text


def test_format_plan_context_empty_plan():
    """Empty plan produces empty string."""
    plan = PlanResult(success=False)
    assert format_plan_context(plan) == ""
