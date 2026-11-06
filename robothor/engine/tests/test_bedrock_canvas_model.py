"""A Bedrock model can be the canvas model.

The dashboard canvas wants the fastest model the instance can reach, and the
fastest measured for a full HTML render was Amazon Nova 2 Lite on Bedrock. For
an operator to choose it, three things have to hold:

1. it has a curated row, because the Providers page refuses to save a model id
   the engine cannot name, and a model with no row is sized and priced as a
   generic 128K one;
2. Bedrock reports the model back without the ``bedrock/`` routing prefix, and
   cost accounting looks up the id the response carries;
3. both canvas calls are sent without the parameters this route has not been
   measured with: no thinking block, no reasoning effort, no cache markers.
   The render is a plain completion; the triage before it asks for a JSON
   object, which the model answers (measured 2026-11, inside a markdown fence
   the dashboard already strips).
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from robothor.engine.llm_client import LLMClient, llm_call
from robothor.engine.model_registry import (
    _MODEL_REGISTRY,
    get_model_limits,
    supports_cache_control,
)

NOVA = "bedrock/us.amazon.nova-2-lite-v1:0"
#: What Bedrock puts in ``response.model``.
NOVA_REPORTED = "us.amazon.nova-2-lite-v1:0"
MESSAGES = [
    {"role": "system", "content": "You draw the canvas."},
    {"role": "user", "content": "Draw it."},
]


@pytest.fixture
def captured(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return litellm.ModelResponse(model=NOVA_REPORTED)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


@pytest.mark.parametrize("model", [NOVA, NOVA_REPORTED])
def test_the_model_has_a_curated_row_under_both_ids(model):
    assert model in _MODEL_REGISTRY
    limits = get_model_limits(model)
    # US cross-Region profile, per litellm's catalog (2026-10).
    assert (
        limits.max_input_tokens,
        limits.max_output_tokens,
        limits.input_cost_per_token,
        limits.output_cost_per_token,
        limits.cache_read_cost_per_token,
    ) == (1_000_000, 64_000, 3.3e-07, 2.75e-06, 8.25e-08)
    assert limits.status == "current"


def test_the_reported_id_is_the_same_row():
    assert get_model_limits(NOVA_REPORTED) is get_model_limits(NOVA)


def test_the_providers_page_can_save_it():
    from robothor.engine.admin_providers import _routable_model_ids

    assert NOVA in _routable_model_ids()


def test_the_route_gets_no_cache_markers(monkeypatch):
    # Even though litellm's catalog says the model caches prompts.
    monkeypatch.setattr(
        "robothor.engine.model_registry._cache_control_from_litellm", lambda _m: True
    )
    assert supports_cache_control(NOVA) is False
    kwargs = LLMClient._build_llm_kwargs(NOVA, MESSAGES, [], 100, 0.3)
    assert "cache_control" not in repr(kwargs["messages"])


def test_the_route_is_sent_no_reasoning_parameters():
    kwargs = LLMClient._build_llm_kwargs(NOVA, MESSAGES, [], 100, 0.3)
    assert "thinking" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["temperature"] == 0.3


async def test_a_canvas_render_is_a_plain_completion(captured):
    await llm_call(MESSAGES, model=[NOVA], temperature=0.3, max_tokens=4096)
    sent = captured[-1]
    assert sent["model"] == NOVA
    assert sent["max_tokens"] == 4096
    assert sent["temperature"] == 0.3
    assert not {"thinking", "reasoning_effort", "service_tier"} & set(sent)


async def test_a_canvas_triage_asks_for_a_json_object(captured):
    await llm_call(MESSAGES, model=[NOVA], temperature=0.1, json_mode=True, max_tokens=256)
    sent = captured[-1]
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["max_tokens"] == 256
    assert not {"thinking", "reasoning_effort", "service_tier"} & set(sent)


def test_the_reported_id_dials_bedrock_too():
    """The cost-lookup alias is listed with every other registry id, so the
    Providers page can save it. Dialled as written, it must still reach
    Bedrock (a real call with it answered, 2026-11)."""
    assert litellm.get_llm_provider(NOVA_REPORTED)[1] == "bedrock"
