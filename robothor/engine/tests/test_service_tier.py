"""An agent can ask for the provider's priority lane.

Shared pay-as-you-go capacity is first come, first served: when a provider is
busy, a standard request waits behind everyone else's and an interactive turn
stalls for half a minute. OpenAI sells a faster lane per request
(``service_tier="priority"``). ``model.service_tier: priority`` in a manifest,
or in the fleet defaults, puts every OpenAI call a run makes in that lane: the
agent loop, and the planner, verifier and compaction calls around it.

Routes with no such lane are sent nothing: an unknown parameter is refused by
some providers and silently dropped by others, and neither is a priority lane.
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from robothor.engine.config import manifest_to_agent_config
from robothor.engine.llm_client import LLMClient, llm_call
from robothor.engine.manifest_schema import validate
from robothor.engine.model_registry import current_service_tier, set_service_tier
from robothor.engine.models import AgentConfig

OPENAI_MODEL = "openai/gpt-6-luna"
MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look something up.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
#: Routes with no priority lane the engine knows how to ask for.
NO_LANE = [
    "vertex_ai/gemini-3.8-flash",
    "bedrock/us.amazon.nova-2-lite-v1:0",
    "anthropic/claude-sonnet-4.6",
    # OpenRouter's OpenAI route speaks OpenRouter's dialect, not OpenAI's.
    "openrouter/openai/gpt-5.4",
    "ollama_chat/qwen3.8:27b",
]


@pytest.fixture(autouse=True)
def _default_tier():
    set_service_tier("default")
    yield
    set_service_tier("default")


@pytest.fixture
def captured(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs.get("model"),
            choices=[{"message": {"role": "assistant", "content": '{"passed": true}'}}],
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


def _manifest(**model: Any) -> dict[str, Any]:
    return {
        "id": "alice-agent",
        "name": "Alice's agent",
        "model": {"primary": OPENAI_MODEL, **model},
    }


# ─── The agent loop ────────────────────────────────────────────────────────


def test_priority_reaches_an_openai_agent_call():
    set_service_tier("priority")
    kwargs = LLMClient._build_llm_kwargs(OPENAI_MODEL, MESSAGES, TOOLS, 100, 0.3)
    assert kwargs["service_tier"] == "priority"


def test_a_bare_openai_id_is_the_same_route():
    set_service_tier("priority")
    kwargs = LLMClient._build_llm_kwargs("gpt-6-luna", MESSAGES, TOOLS, 100, 0.3)
    assert kwargs["service_tier"] == "priority"


def test_the_default_tier_sends_nothing():
    """Omitting the parameter is the standard lane; nothing changes for a
    fleet that never asks."""
    kwargs = LLMClient._build_llm_kwargs(OPENAI_MODEL, MESSAGES, TOOLS, 100, 0.3)
    assert "service_tier" not in kwargs


@pytest.mark.parametrize("model", NO_LANE)
def test_a_route_without_a_priority_lane_is_sent_nothing(model):
    set_service_tier("priority")
    kwargs = LLMClient._build_llm_kwargs(model, MESSAGES, TOOLS, 100, 0.3)
    assert "service_tier" not in kwargs


# ─── The calls around the loop ─────────────────────────────────────────────


async def test_priority_reaches_the_verifier(captured):
    from robothor.engine.verifier import verify_output

    set_service_tier("priority")
    await verify_output("PONG", "says PONG", 0, OPENAI_MODEL)
    assert captured[-1]["service_tier"] == "priority"


async def test_priority_reaches_a_plain_llm_call(captured):
    set_service_tier("priority")
    await llm_call(MESSAGES, model=OPENAI_MODEL)
    assert captured[-1]["service_tier"] == "priority"


async def test_a_chain_walk_asks_each_model_for_its_own_lane(captured, monkeypatch):
    """The fallback must not inherit the parameter the primary was sent."""

    async def refuse_openai(**kwargs: Any) -> Any:
        captured.append(kwargs)
        if kwargs["model"] == OPENAI_MODEL:
            raise litellm.exceptions.APIConnectionError(
                message="down", llm_provider="openai", model=OPENAI_MODEL
            )
        return litellm.ModelResponse(model=kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", refuse_openai)
    set_service_tier("priority")
    await llm_call(MESSAGES, model=[OPENAI_MODEL, "vertex_ai/gemini-3.8-flash"])
    by_model = {call["model"]: call for call in captured}
    assert by_model[OPENAI_MODEL]["service_tier"] == "priority"
    assert "service_tier" not in by_model["vertex_ai/gemini-3.8-flash"]


async def test_the_auxiliary_calls_stay_on_the_default_lane_by_default(captured):
    from robothor.engine.verifier import verify_output

    await verify_output("PONG", "says PONG", 0, OPENAI_MODEL)
    assert "service_tier" not in captured[-1]


# ─── Where the setting comes from ──────────────────────────────────────────


def test_the_manifest_sets_the_agents_tier():
    assert manifest_to_agent_config(_manifest(service_tier="priority")).service_tier == "priority"


def test_an_agent_that_does_not_ask_is_on_the_default_tier():
    assert manifest_to_agent_config(_manifest()).service_tier == "default"
    assert AgentConfig(id="bob-agent", name="Bob's agent").service_tier == "default"


def test_the_schema_knows_the_field_and_its_values():
    codes = {issue.code for issue in validate(_manifest(service_tier="priority"))}
    assert not codes & {"unknown_key", "invalid_enum"}

    issues = validate(_manifest(service_tier="turbo"))
    assert [i.code for i in issues if "service_tier" in i.path] == ["invalid_enum"]


def test_a_run_carries_its_agents_tier():
    """The runner publishes the agent's settings through this at run start."""
    from robothor.engine.model_registry import apply_run_model_settings

    apply_run_model_settings(
        AgentConfig(id="alice-agent", name="Alice's agent", service_tier="priority")
    )
    assert current_service_tier() == "priority"


def test_an_unset_tier_reads_as_default():
    set_service_tier("")
    assert current_service_tier() == "default"
