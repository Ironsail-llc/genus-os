"""Tests for the planning phase."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.planner import (
    PlanResult,
    _normalize_steps,
    format_plan_context,
    generate_plan,
    replan,
)


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
        result = await generate_plan(
            "Classify inbox", ["read_file", "exec", "create_task"], "test-model"
        )

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
    assert result.error is not None
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


# ── Step normalization (truncated / malformed LLM plans) ─────────────


def _mock_response(payload: str, finish_reason: str = "stop") -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload
    response.choices[0].finish_reason = finish_reason
    return response


# Exact shape of the Aug 18 production crash: valid JSON whose plan array
# carries a trailing garbage string ('risks: [') from a truncated JSON-mode
# response that the provider's constrained decoding repaired into valid JSON.
TRUNCATED_PLAN_PAYLOAD = {
    "difficulty": "moderate",
    "estimated_steps": 6,
    "plan": [
        {"step": 1, "action": "Review stored memories", "tool": "search_memory"},
        {"step": 2, "action": "Identify concrete facts", "tool": "search_memory"},
        {"step": 3, "action": "Store a concrete fact", "tool": "store_memory"},
        {"step": 4, "action": "Verify the write", "tool": "search_memory"},
        {"step": 5, "action": "Summarize findings", "tool": ""},
        {"step": 6, "action": "Report back", "tool": ""},
        "risks: [",
    ],
}


@pytest.mark.asyncio
async def test_generate_plan_truncated_string_step():
    """Aug 18 regression: a string element in the plan array must be
    normalized to a dict step, and formatting must not raise."""
    response = _mock_response(json.dumps(TRUNCATED_PLAN_PAYLOAD))

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Store a fact", ["search_memory"], "test-model")

    assert result.success is True
    assert len(result.plan) == 7
    assert all(isinstance(step, dict) for step in result.plan)
    assert result.plan[6] == {"step": 7, "action": "risks: [", "tool": ""}
    # The original crash site: format_plan_context on the parsed plan.
    text = format_plan_context(result)
    assert "risks: [" in text


@pytest.mark.asyncio
async def test_generate_plan_string_only_plan():
    """A plan array of bare strings is wrapped into dict steps."""
    payload = {
        "difficulty": "simple",
        "estimated_steps": 2,
        "plan": ["Read the file", "Summarize it"],
    }
    response = _mock_response(json.dumps(payload))

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Summarize", ["read_file"], "test-model")

    assert result.plan == [
        {"step": 1, "action": "Read the file", "tool": ""},
        {"step": 2, "action": "Summarize it", "tool": ""},
    ]
    assert format_plan_context(result)  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_plan", ["do the thing", {"step": 1}, 42, None])
async def test_generate_plan_non_list_plan_becomes_empty(bad_plan):
    """A plan that is not a list normalizes to []."""
    payload = {"difficulty": "simple", "estimated_steps": 1, "plan": bad_plan}
    response = _mock_response(json.dumps(payload))

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Do stuff", ["tool1"], "test-model")

    assert result.plan == []


@pytest.mark.asyncio
async def test_generate_plan_drops_junk_elements():
    """Non-dict, non-string junk and empty strings are dropped."""
    payload = {
        "difficulty": "simple",
        "estimated_steps": 3,
        "plan": [{"step": 1, "action": "Real step", "tool": "exec"}, None, 42, "", "  "],
    }
    response = _mock_response(json.dumps(payload))

    with patch("litellm.acompletion", return_value=response):
        result = await generate_plan("Do stuff", ["exec"], "test-model")

    assert result.plan == [{"step": 1, "action": "Real step", "tool": "exec"}]


@pytest.mark.asyncio
async def test_generate_plan_warns_on_truncated_response(caplog):
    """finish_reason == 'length' logs a truncation warning."""
    response = _mock_response(json.dumps(TRUNCATED_PLAN_PAYLOAD), finish_reason="length")

    with (
        patch("litellm.acompletion", return_value=response),
        caplog.at_level(logging.WARNING, logger="robothor.engine.planner"),
    ):
        result = await generate_plan("Store a fact", ["search_memory"], "test-model")

    assert result.success is True
    assert any("truncat" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_replan_normalizes_steps():
    """replan() applies the same normalization at its parse site."""
    from robothor.engine.scratchpad import Scratchpad

    original = PlanResult(
        success=True,
        plan=[{"step": 1, "action": "Read", "tool": "read_file"}],
    )
    sp = Scratchpad()
    sp.set_plan(original.plan)

    payload = {
        "difficulty": "moderate",
        "estimated_steps": 2,
        "plan": [{"step": 1, "action": "Retry read", "tool": "read_file"}, "risks: ["],
    }
    response = _mock_response(json.dumps(payload))

    with patch("litellm.acompletion", return_value=response):
        result = await replan(original, sp, "test-model")

    assert result.success is True
    assert all(isinstance(step, dict) for step in result.plan)
    assert result.plan[1] == {"step": 2, "action": "risks: [", "tool": ""}
    assert format_plan_context(result)  # must not raise


def test_normalize_steps_unit():
    """_normalize_steps: dicts kept, strings wrapped, junk dropped, non-list → []."""
    assert _normalize_steps(None) == []
    assert _normalize_steps("a plan") == []
    assert _normalize_steps({"step": 1}) == []
    assert _normalize_steps([]) == []
    assert _normalize_steps([{"step": 1, "action": "a", "tool": "t"}, "loose", 3, None, ""]) == [
        {"step": 1, "action": "a", "tool": "t"},
        {"step": 2, "action": "loose", "tool": ""},
    ]


def test_format_plan_context_non_dict_step_defense():
    """Defense in depth: a hand-built PlanResult with a non-dict step renders
    a plain line instead of raising AttributeError."""
    plan = PlanResult(
        success=True,
        difficulty="moderate",
        plan=[{"step": 1, "action": "Read file", "tool": "read_file"}, "risks: ["],
    )
    text = format_plan_context(plan)
    assert "Read file" in text
    assert "risks: [" in text


def test_scratchpad_handles_normalized_string_step():
    """Latent scratchpad crash sites (.get on steps) are safe once steps
    are normalized: wrapped string steps track progress without raising."""
    from robothor.engine.scratchpad import Scratchpad

    steps = _normalize_steps([{"step": 1, "action": "Read", "tool": "read_file"}, "risks: ["])
    sp = Scratchpad(inject_interval=1)
    sp.set_plan(steps)
    sp.record_tool_call("read_file")  # _try_advance_step: step.get("tool")
    sp.record_tool_call("other_tool", error="boom")  # _step_attempts path
    summary = sp.format_summary()  # format_summary: step.get("tool"/"action")
    assert "[WORKING STATE]" in summary


# ── Planner is non-fatal end to end ──────────────────────────────────


@pytest.mark.asyncio
async def test_plan_context_formatting_failure_is_nonfatal(engine_config, mock_db):
    """If plan-context formatting raises, the run proceeds without plan
    context instead of failing (Aug 18 regression: AttributeError escaped
    the planner's non-fatal guard and aborted the whole run)."""
    from unittest.mock import AsyncMock

    from robothor.engine.models import AgentConfig
    from robothor.engine.runner import AgentRunner

    runner = AgentRunner(engine_config)

    plan = PlanResult(
        success=True,
        plan=[{"step": 1, "action": "Do it", "tool": ""}],
    )

    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = "Done"
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage

    async def fake_completion(**kwargs):
        return response

    agent_config = AgentConfig(
        id="plan-test",
        name="Plan Test",
        model_primary="test-model",
        planning_enabled=True,
    )

    with (
        patch("litellm.acompletion", side_effect=fake_completion),
        patch.object(runner.registry, "build_for_agent", return_value=[]),
        patch.object(runner.registry, "get_tool_names", return_value=[]),
        patch.object(runner, "_run_planner", AsyncMock(return_value=plan)),
        patch(
            "robothor.engine.planner.format_plan_context",
            side_effect=RuntimeError("formatting exploded"),
        ),
    ):
        run = await runner.execute("plan-test", "do stuff", agent_config=agent_config)

    assert run.status.value == "completed"
