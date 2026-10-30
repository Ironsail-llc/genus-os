"""Vertex/Gemini call shape, and short auxiliary calls on reasoning models.

Three defects kept a Vertex Gemini primary from ever answering:

1. The system prompt was marked ``cache_control`` because litellm's catalog
   says the model supports prompt caching. For ``vertex_ai/`` and ``gemini/``
   litellm turns that marker into Gemini *explicit* context caching, which the
   API refuses below 4,096 tokens and always alongside tools or a system
   instruction — so every agent call came back 400. Gemini caches implicitly;
   it needs no markers.
2. A verification retry ends ``[..., assistant, developer]``. litellm hoists
   developer/system turns into Gemini's ``system_instruction``, so the request
   ended on the model turn and Gemini refused it ("Requests ending with a model
   turn are not supported"). The Anthropic leg already rewrote those turns as
   user turns; Gemini needs the same.
3. The planner (500 tokens) and verifier (300) caps were spent entirely on
   reasoning by reasoning models, returning truncated or empty JSON — and an
   empty verdict once failed a correct answer and forced a second full turn.
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from robothor.engine.llm_client import (
    REASONING_ANSWER_HEADROOM,
    LLMClient,
    short_answer_kwargs,
)
from robothor.engine.model_registry import (
    _MODEL_REGISTRY,
    set_reasoning_effort,
    supports_cache_control,
)
from robothor.engine.session import ENGINE_CONTEXT_ROLE

VERTEX_MODEL = "vertex_ai/gemini-3.8-flash"
OPENAI_MODEL = "openai/gpt-5.6-luna"
ANTHROPIC_DIRECT = "anthropic/claude-sonnet-4.6"
SYSTEM = [
    {"role": "system", "content": "You are a helpful agent."},
    {"role": "user", "content": "hi"},
]


@pytest.fixture(autouse=True)
def _default_effort():
    set_reasoning_effort("medium")
    yield
    set_reasoning_effort("medium")


def _has_cache_marker(kwargs: dict[str, Any]) -> bool:
    return "cache_control" in repr(kwargs["messages"])


# ─── R2-D1: no explicit-cache markers for Gemini ──────────────────────────


@pytest.mark.parametrize(
    "model",
    [VERTEX_MODEL, "vertex_ai/gemini-2.5-pro", "gemini/gemini-2.5-flash", "gemini/gemini-3-pro"],
)
def test_gemini_routes_never_get_cache_control(model, monkeypatch):
    # Even where litellm's catalog says the model caches prompts.
    monkeypatch.setattr(
        "robothor.engine.model_registry._cache_control_from_litellm", lambda _m: True
    )
    assert supports_cache_control(model) is False
    kwargs = LLMClient._build_llm_kwargs(model, SYSTEM, [], 100, 0.3)
    assert not _has_cache_marker(kwargs)
    assert kwargs["messages"][0]["content"] == "You are a helpful agent."


def test_the_vertex_row_pins_cache_control_off():
    assert _MODEL_REGISTRY[VERTEX_MODEL].supports_cache_control is False
    # Deliberately: the engine sends this route no thinking block.
    assert _MODEL_REGISTRY[VERTEX_MODEL].supports_thinking is False


def test_anthropic_still_gets_cache_control(monkeypatch):
    monkeypatch.setattr(
        "robothor.engine.model_registry._cache_control_from_litellm", lambda _m: True
    )
    kwargs = LLMClient._build_llm_kwargs(ANTHROPIC_DIRECT, SYSTEM, [], 100, 0.3)
    assert _has_cache_marker(kwargs)


# ─── R2-D2: the request never ends on a model turn ────────────────────────


RETRY_HISTORY = [
    {"role": "system", "content": "You are a helpful agent."},
    {"role": "user", "content": "Say PONG."},
    {"role": "assistant", "content": "PONG"},
    {"role": ENGINE_CONTEXT_ROLE, "content": "Verification failed: try again."},
]


@pytest.mark.parametrize("model", [VERTEX_MODEL, "gemini/gemini-2.5-flash"])
def test_gemini_verification_retry_ends_on_a_user_turn(model):
    messages = LLMClient._build_llm_kwargs(model, RETRY_HISTORY, [], 100, 0.3)["messages"]
    non_system = [m for m in messages if m["role"] != "system"]
    assert non_system[-1]["role"] == "user"
    assert "Verification failed" in repr(non_system[-1]["content"])
    assert not any(m["role"] == ENGINE_CONTEXT_ROLE for m in messages)


@pytest.mark.parametrize("model", [VERTEX_MODEL, "gemini/gemini-2.5-flash"])
def test_gemini_trailing_assistant_is_not_sent_last(model):
    history = [*RETRY_HISTORY[:3]]
    messages = LLMClient._build_llm_kwargs(model, history, [], 100, 0.3)["messages"]
    assert messages[-1]["role"] == "user"


def test_openai_keeps_the_developer_role():
    """OpenAI accepts ``developer`` verbatim; its payload must not change."""
    messages = LLMClient._build_llm_kwargs(OPENAI_MODEL, RETRY_HISTORY, [], 100, 0.3)["messages"]
    assert messages[-1]["role"] == ENGINE_CONTEXT_ROLE


# ─── R2-D3: short auxiliary answers on reasoning models ───────────────────


def test_short_answer_on_openai_reasoning_is_low_effort_with_headroom():
    assert short_answer_kwargs(OPENAI_MODEL, 300) == {
        "max_tokens": 300 + REASONING_ANSWER_HEADROOM,
        "reasoning_effort": "low",
    }


def test_short_answer_on_gemini_thinking_model_raises_the_cap_only():
    """No reasoning parameter: litellm's bundled catalog does not know the
    model reasons and refuses one, so the ceiling is what moves."""
    assert short_answer_kwargs(VERTEX_MODEL, 300) == {
        "max_tokens": 300 + REASONING_ANSWER_HEADROOM,
    }


@pytest.mark.parametrize("model", ["openai/gpt-4o", ANTHROPIC_DIRECT, "openrouter/x/y"])
def test_short_answer_on_other_models_is_unchanged(model):
    assert short_answer_kwargs(model, 300) == {"max_tokens": 300}


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


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (OPENAI_MODEL, {"max_tokens": 300 + REASONING_ANSWER_HEADROOM, "reasoning_effort": "low"}),
        (VERTEX_MODEL, {"max_tokens": 300 + REASONING_ANSWER_HEADROOM, "temperature": 0.1}),
        ("openai/gpt-4o", {"max_tokens": 300, "temperature": 0.1}),
    ],
)
async def test_verifier_sizes_its_call_for_the_model(captured, model, expected):
    from robothor.engine.verifier import verify_output

    await verify_output("PONG", "says PONG", 0, model)
    sent = captured[-1]
    assert {k: sent[k] for k in expected} == expected
    if "reasoning_effort" not in expected:
        assert "reasoning_effort" not in sent
    if "temperature" not in expected:
        assert "temperature" not in sent


@pytest.mark.parametrize(
    ("model", "cap"),
    [(OPENAI_MODEL, 500 + REASONING_ANSWER_HEADROOM), ("openai/gpt-4o", 500)],
)
async def test_planner_sizes_its_call_for_the_model(captured, model, cap):
    from robothor.engine.planner import generate_plan

    await generate_plan("Say PONG", ["lookup"], model)
    assert captured[-1]["max_tokens"] == cap
    assert ("reasoning_effort" in captured[-1]) is (model == OPENAI_MODEL)
