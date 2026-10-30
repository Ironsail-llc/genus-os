"""OpenAI reasoning models get ``reasoning_effort`` and no ``temperature``.

A chain of ``vertex_ai/gemini-3.8-flash`` with ``openai/gpt-5.6-luna`` as the
fallback never answered on the fallback, although a direct litellm call to the
same model did. The engine's kwargs were wrong for the OpenAI route three ways:

1. With no curated registry row, the model was not known to reason, so tools
   went out without ``reasoning_effort`` and OpenAI refused the call.
2. A row with ``supports_thinking=True`` made the engine send the
   Anthropic-style ``thinking={...}`` block, which litellm refuses for OpenAI
   before the request leaves the process.
3. ``temperature=0.3`` rode along, and GPT-5-family reasoning calls reject any
   sampling temperature.

These tests capture what actually reaches ``litellm.acompletion`` per provider.
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from robothor.engine.llm_client import (
    LLMClient,
    _thinking_budget,
    is_openai_reasoning_model,
    llm_call,
)
from robothor.engine.model_registry import (
    _MODEL_REGISTRY,
    get_model_limits,
    set_reasoning_effort,
)

OPENAI_MODEL = "openai/gpt-5.6-luna"
ANTHROPIC_MODEL = "openrouter/anthropic/claude-opus-4.7"
VERTEX_MODEL = "vertex_ai/gemini-3.8-flash"
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


@pytest.fixture(autouse=True)
def _default_effort():
    set_reasoning_effort("medium")
    yield
    set_reasoning_effort("medium")


@pytest.fixture
def captured(monkeypatch) -> list[dict[str, Any]]:
    """A fake ``litellm.acompletion`` that records the kwargs it was handed."""
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return litellm.ModelResponse(model=kwargs.get("model"))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


async def _sent(model: str, captured: list[dict[str, Any]], **build: Any) -> dict[str, Any]:
    kwargs = LLMClient._build_llm_kwargs(model, MESSAGES, TOOLS, 100, 0.3, **build)
    await LLMClient()._call_with_image_fallback(model=model, messages=MESSAGES, kwargs=kwargs)
    sent = dict(captured[-1])
    sent.pop("messages")
    sent.pop("timeout")
    return sent


async def test_openai_reasoning_model_gets_reasoning_effort_and_no_temperature(captured):
    assert await _sent(OPENAI_MODEL, captured) == {
        "model": OPENAI_MODEL,
        "max_tokens": 16_384,
        "tools": TOOLS,
        "tool_choice": "auto",
        "reasoning_effort": "medium",
    }


async def test_anthropic_thinking_block_is_unchanged(captured):
    sent = await _sent(ANTHROPIC_MODEL, captured)
    assert sent == {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 16_384,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 1.0,
        "thinking": {"type": "enabled", "budget_tokens": _thinking_budget(16_384, "medium")},
        "extra_body": sent["extra_body"],
    }
    assert sent["extra_body"]["provider"]["order"] == ["Anthropic"]


async def test_vertex_gemini_keeps_temperature_and_gets_no_reasoning_params(captured):
    assert await _sent(VERTEX_MODEL, captured) == {
        "model": VERTEX_MODEL,
        "max_tokens": 16_384,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
    }


async def test_bare_openai_id_is_treated_the_same(captured):
    sent = await _sent("gpt-5.6-luna", captured)
    assert sent["reasoning_effort"] == "medium"
    assert "temperature" not in sent
    assert "thinking" not in sent


async def test_openai_reasoning_model_without_a_registry_row_still_reasons(captured):
    """Defect 1: the catalog says it reasons even where the registry is silent."""
    assert "openai/gpt-5.5" not in _MODEL_REGISTRY
    sent = await _sent("openai/gpt-5.5", captured)
    assert sent["reasoning_effort"] == "medium"
    assert "temperature" not in sent
    assert "thinking" not in sent


async def test_openai_non_reasoning_model_is_left_alone(captured):
    sent = await _sent("openai/gpt-4o", captured)
    assert sent["temperature"] == 0.3
    assert "reasoning_effort" not in sent
    assert "thinking" not in sent


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
async def test_agent_reasoning_effort_reaches_openai(captured, monkeypatch, effort, expected):
    """``max`` is ``high`` unless the catalog says the model offers ``xhigh``,
    because litellm refuses ``xhigh`` for a model it cannot vouch for.

    The catalog row is pinned here rather than read from the bundled one: the
    bundled catalog changes with the litellm release (1.92.2 did not know
    gpt-5.6-luna; 1.100.4 lists it with xhigh), and this test is about the
    fallback, not about one release's catalog. The xhigh side is
    ``test_max_asks_for_xhigh_where_the_catalog_offers_it``."""
    monkeypatch.setitem(
        litellm.model_cost,
        "gpt-5.6-luna",
        {"litellm_provider": "openai", "supports_xhigh_reasoning_effort": False},
    )
    set_reasoning_effort(effort)
    assert (await _sent(OPENAI_MODEL, captured))["reasoning_effort"] == expected


async def test_max_asks_for_xhigh_where_the_catalog_offers_it(captured, monkeypatch):
    monkeypatch.setitem(
        litellm.model_cost,
        "gpt-5.6-luna",
        {"litellm_provider": "openai", "supports_xhigh_reasoning_effort": True},
    )
    set_reasoning_effort("max")
    assert (await _sent(OPENAI_MODEL, captured))["reasoning_effort"] == "xhigh"


async def test_reasoning_only_re_ask_asks_for_less_effort(captured):
    set_reasoning_effort("high")
    sent = await _sent(OPENAI_MODEL, captured, thinking_reduced=True)
    assert sent["reasoning_effort"] == "low"


def test_detection():
    assert is_openai_reasoning_model("openai/gpt-5.6-luna")
    assert is_openai_reasoning_model("gpt-5.6-luna")
    assert not is_openai_reasoning_model("openai/gpt-4o")
    assert not is_openai_reasoning_model(ANTHROPIC_MODEL)
    assert not is_openai_reasoning_model(VERTEX_MODEL)
    # OpenRouter's OpenAI route is a different API shape; not this fix's call.
    assert not is_openai_reasoning_model("openrouter/openai/gpt-5.4")
    # codex/* is a subscription provider with its own adapter.
    assert not is_openai_reasoning_model("codex/gpt-5.5")


async def test_auxiliary_llm_call_drops_temperature_for_openai_reasoning(captured):
    await llm_call(MESSAGES, model=OPENAI_MODEL, temperature=0.1)
    assert "temperature" not in captured[-1]
    await llm_call(MESSAGES, model=VERTEX_MODEL, temperature=0.1)
    assert captured[-1]["temperature"] == 0.1


async def test_pooled_completion_drops_temperature_for_openai_reasoning(captured):
    from robothor.engine.pooled_completion import acompletion

    await acompletion(model=OPENAI_MODEL, messages=MESSAGES, temperature=0.1)
    assert "temperature" not in captured[-1]
    await acompletion(model=VERTEX_MODEL, messages=MESSAGES, temperature=0.1)
    assert captured[-1]["temperature"] == 0.1


#: litellm's published catalog (2026-10). The engine pins litellm to its
#: BUNDLED price list, which predates both models — exactly why they need rows.
CATALOG = {
    "vertex_ai/gemini-3.8-flash": (1_048_576, 65_536, 7.5e-07, 3.75e-06, 7.5e-08),
    "openai/gpt-5.6-luna": (922_000, 128_000, 2e-07, 1.2e-06, 2e-08),
    "gpt-5.6-luna": (922_000, 128_000, 2e-07, 1.2e-06, 2e-08),
}


@pytest.mark.parametrize("model", sorted(CATALOG))
def test_fleet_models_have_curated_rows_priced_from_the_catalog(model):
    """Without a row, cost tracking priced these at 0.0 and logged them unknown."""
    assert model in _MODEL_REGISTRY
    limits = get_model_limits(model)
    assert (
        limits.max_input_tokens,
        limits.max_output_tokens,
        limits.input_cost_per_token,
        limits.output_cost_per_token,
        limits.cache_read_cost_per_token,
    ) == CATALOG[model]
    assert limits.accepts_images
