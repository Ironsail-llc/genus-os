"""The compaction re-ask must speak the provider's reasoning dialect.

Journal 2026-09-13 19:45 ET: `_re_ask_for_the_answer` put a TOP-LEVEL
``reasoning_effort`` on the re-ask. litellm maps that knob per route, and for
``openrouter/xiaomi/mimo-v2.5`` it does not — every re-ask on the fleet primary
died with ``litellm.UnsupportedParamsError: openrouter does not support
parameters: ['reasoning_effort']`` before it reached the provider, so
compaction fell straight through to the chain walk the re-ask exists to
prevent (a 30-character summary of an 81k-token context). DeepSeek, which
litellm does map, made the defect look like it worked.

The run path never had the bug: it asks ``llm_client`` for the thinking block,
which is gated on the model's own ``supports_thinking`` and shaped for the
provider. These tests pin the re-ask to that SAME builder — equality against
its output, so the two cannot drift apart again.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import litellm
import pytest

from robothor.engine import compaction
from robothor.engine.llm_client import _thinking_kwargs, thinking_kwargs_for_call

#: Registered with ``supports_thinking=True`` — the run path builds it a block.
REASONING_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
#: The fleet primary, registered ``supports_thinking=False``, and the route
#: whose rejection of ``reasoning_effort`` is the defect under test.
MIMO_MODEL = "openrouter/xiaomi/mimo-v2.5"

#: Large enough that a reduced block clears ``MIN_THINKING_BUDGET``, so
#: "no bare knob" and "no block at all" are distinguishable outcomes.
BIG_MAX_TOKENS = 16_384


def _response(content: str | None = "a summary") -> Any:
    message = SimpleNamespace(content=content, tool_calls=None)
    usage = SimpleNamespace(
        prompt_tokens=900,
        completion_tokens=40,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=None),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
        model=REASONING_MODEL,
    )


def _re_ask_kwargs(model: str, max_tokens: int = BIG_MAX_TOKENS) -> dict[str, Any]:
    """Run one re-ask against a recording fake and return what it dialled."""
    seen: list[dict[str, Any]] = []

    async def _fake(model: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return _response()

    with patch("robothor.engine.compaction.pooled_acompletion", new=_fake):
        asyncio.run(
            compaction._re_ask_for_the_answer(
                model,
                {
                    "messages": [{"role": "user", "content": "summarise"}],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
                time.monotonic(),
            )
        )
    assert seen, "the re-ask never dialled"
    return seen[0]


def test_re_ask_never_sends_a_bare_reasoning_effort() -> None:
    """The exact kwarg litellm rejected for the OpenRouter MiMo route."""
    for model in (MIMO_MODEL, REASONING_MODEL):
        assert "reasoning_effort" not in _re_ask_kwargs(model), (
            f"{model}: a top-level reasoning_effort is what UnsupportedParamsError killed"
        )


def test_re_ask_carries_no_thinking_for_a_model_without_reasoning_support() -> None:
    """MiMo is registered ``supports_thinking=False`` — it gets nothing."""
    kwargs = _re_ask_kwargs(MIMO_MODEL)
    assert "thinking" not in kwargs
    assert "reasoning" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_re_ask_thinking_block_equals_the_run_paths_reduced_block() -> None:
    """One builder, asserted by equality so the two cannot drift."""
    expected = _thinking_kwargs(REASONING_MODEL, BIG_MAX_TOKENS, reduced=True)
    assert expected, "the fixture must be large enough to earn a block, or this proves nothing"
    kwargs = _re_ask_kwargs(REASONING_MODEL)
    assert {k: kwargs[k] for k in expected} == expected


def test_the_shared_builder_is_the_one_the_run_path_gates_on() -> None:
    """``supports_thinking`` decides, not the caller — both sides ask it."""
    assert thinking_kwargs_for_call(MIMO_MODEL, BIG_MAX_TOKENS, reduced=True) == {}
    assert thinking_kwargs_for_call(
        REASONING_MODEL, BIG_MAX_TOKENS, reduced=True
    ) == _thinking_kwargs(REASONING_MODEL, BIG_MAX_TOKENS, reduced=True)


@pytest.mark.asyncio
async def test_a_provider_that_rejects_reasoning_effort_still_gets_its_re_ask() -> None:
    """The live failure: the re-ask must survive, and the chain stay unwalked."""
    calls: list[str] = []
    reasoned = {"done": False}

    async def _fake(model: str, **kwargs: Any) -> Any:
        calls.append(model)
        if "reasoning_effort" in kwargs:
            raise litellm.UnsupportedParamsError(
                status_code=400,
                message="openrouter does not support parameters: ['reasoning_effort']",
                model=model,
                llm_provider="openrouter",
            )
        if not reasoned["done"]:
            reasoned["done"] = True
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=None,
                            reasoning_content="weighing the dedup keys…",
                        ),
                        finish_reason="length",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=900,
                    completion_tokens=9800,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=9800),
                ),
                model=model,
            )
        return _response()

    with patch("robothor.engine.compaction.pooled_acompletion", new=_fake):
        response = await compaction._acompletion_over_chain(
            [MIMO_MODEL, "openrouter/second", "ollama_chat/qwen3.8:27b"],
            messages=[{"role": "user", "content": "summarise"}],
            temperature=0.1,
            max_tokens=BIG_MAX_TOKENS,
        )

    assert response.choices[0].message.content == "a summary"
    assert calls == [MIMO_MODEL, MIMO_MODEL], f"the chain must not be walked, got {calls}"
