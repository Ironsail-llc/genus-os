"""Router v2: a long chat message no longer buys a separate planner LLM call.

Today any message over 500 characters is classified ``complex`` and the
complex preset requests planning, so every long Telegram/webchat/plugin-channel
turn pays for an auxiliary planner call before the tool loop starts. Behind
``ROBOTHOR_ROUTER_V2`` that length heuristic stops forcing the planner for
interactive triggers; planning is still requested by plan mode, by the agent's
manifest, or by a cheap LLM-free signal that the message lists several steps.

The LLM is a fake: every ``litellm.acompletion`` call is recorded, and a call
carrying ``response_format=json_object`` is the planner's (the tool loop never
asks for JSON mode).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType

LONG_PROSE = (
    "I have been thinking about the quarterly review and I would like your view "
    "on whether the onboarding notes we wrote last month still read well for a "
    "new member of the team who has never seen the dashboard before. "
) * 3
assert len(LONG_PROSE) > 500


def _numbered(n: int) -> str:
    steps = "\n".join(f"{i}. Do part {i} of the job." for i in range(1, n + 1))
    return "Please help with this.\n" + steps


def _response(content: str) -> Any:
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


class FakeLLM:
    """Records every completion; answers the planner with valid plan JSON."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def planner_calls(self) -> int:
        return sum(1 for c in self.calls if c.get("response_format"))

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("response_format"):
            return _response(
                json.dumps({"difficulty": "complex", "estimated_steps": 3, "plan": []})
            )
        return _response("Done.")


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = []
        registry.build_readonly_for_agent.return_value = []
        registry.get_tool_names.return_value = []
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def chat_agent(sample_agent_config):
    sample_agent_config.planning_enabled = False
    sample_agent_config.planning_model = ""
    sample_agent_config.difficulty_class = ""
    sample_agent_config.task_protocol = False
    return sample_agent_config


async def _turn(runner, agent, message: str, **kwargs: Any) -> tuple[Any, FakeLLM]:
    fake = FakeLLM()
    with (
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=fake.__call__),
    ):
        run = await runner.execute(
            agent.id,
            message,
            agent_config=agent,
            trigger_type=kwargs.pop("trigger_type", TriggerType.WEBCHAT),
            tenant_id="test-tenant",
            user_id="u1",
            user_role="owner",
            **kwargs,
        )
    return run, fake


@pytest.mark.usefixtures("_mock_run_persistence")
class TestLongChatTurn:
    @pytest.mark.asyncio
    async def test_flag_on_long_prose_makes_no_planner_call(
        self, runner, chat_agent, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_ROUTER_V2", "1")
        run, fake = await _turn(runner, chat_agent, LONG_PROSE)
        assert run.status == RunStatus.COMPLETED
        assert fake.planner_calls == 0
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_flag_off_reproduces_todays_planner_call(
        self, runner, chat_agent, monkeypatch
    ):
        monkeypatch.delenv("ROBOTHOR_ROUTER_V2", raising=False)
        run, fake = await _turn(runner, chat_agent, LONG_PROSE)
        assert run.status == RunStatus.COMPLETED
        assert fake.planner_calls == 1
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_flag_on_plan_mode_still_plans(self, runner, chat_agent, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_ROUTER_V2", "1")
        _run, fake = await _turn(runner, chat_agent, LONG_PROSE, readonly_mode=True)
        assert fake.planner_calls == 1

    @pytest.mark.asyncio
    async def test_flag_on_manifest_planning_plans_and_applies_deadline_once(
        self, runner, chat_agent, monkeypatch
    ):
        from robothor.engine.runtime import classified_deadline

        monkeypatch.setenv("ROBOTHOR_ROUTER_V2", "1")
        chat_agent.planning_enabled = True
        applied: list[bool] = []
        real_apply = classified_deadline.apply

        async def counting_apply(*args: Any, **kwargs: Any) -> None:
            applied.append(True)
            await real_apply(*args, **kwargs)

        with (
            patch.object(classified_deadline, "apply", counting_apply),
            patch.object(classified_deadline, "_persist") as persist,
        ):
            _run, fake = await _turn(runner, chat_agent, LONG_PROSE)
        assert fake.planner_calls == 1
        assert len(applied) == 1
        assert persist.call_count <= 1

    @pytest.mark.asyncio
    async def test_flag_on_explicit_steps_still_plan(self, runner, chat_agent, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_ROUTER_V2", "1")
        message = _numbered(3) + "\n" + LONG_PROSE
        _run, fake = await _turn(runner, chat_agent, message)
        assert fake.planner_calls == 1


def _route(cfg, message: str, trigger: TriggerType, **kwargs: Any):
    from robothor.engine.run_lifecycle import RunLifecycleMixin

    return RunLifecycleMixin._apply_routing(None, cfg, message, 0, trigger_type=trigger, **kwargs)


class TestRouteSignal:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_ROUTER_V2", "1")

    @pytest.mark.parametrize(
        "trigger", [TriggerType.TELEGRAM, TriggerType.WEBCHAT, TriggerType.CHANNEL]
    )
    def test_length_alone_does_not_request_planning(self, chat_agent, trigger):
        route = _route(chat_agent, LONG_PROSE, trigger)
        assert route.planning is not True
        # Only the planner is dropped; the rest of the complex preset stays.
        assert route.difficulty == "complex"
        assert route.verification is True

    def test_three_listed_steps_request_planning(self, chat_agent):
        assert _route(chat_agent, _numbered(3), TriggerType.WEBCHAT).planning is True

    def test_two_listed_steps_do_not(self, chat_agent):
        assert _route(chat_agent, _numbered(2), TriggerType.WEBCHAT).planning is not True

    def test_bulleted_steps_count(self, chat_agent):
        message = "Today:\n- book the room\n- send the invite\n- write the agenda"
        assert _route(chat_agent, message, TriggerType.TELEGRAM).planning is True

    def test_sequencing_words_count(self, chat_agent):
        message = "Find the file, then fix the typo, after that run the tests and finally push."
        assert _route(chat_agent, message, TriggerType.WEBCHAT).planning is True

    @pytest.mark.parametrize("trigger", [TriggerType.CRON, TriggerType.WORKFLOW])
    def test_non_interactive_triggers_are_unchanged(self, chat_agent, trigger):
        route = _route(chat_agent, LONG_PROSE, trigger)
        assert route.difficulty == "complex"
        assert route.planning is True

    def test_plan_mode_is_unchanged(self, chat_agent):
        route = _route(chat_agent, LONG_PROSE, TriggerType.WEBCHAT, readonly_mode=True)
        assert route.planning is True

    def test_flag_off_is_unchanged(self, chat_agent, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_ROUTER_V2", raising=False)
        route = _route(chat_agent, LONG_PROSE, TriggerType.WEBCHAT)
        assert route.planning is True
