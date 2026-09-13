"""A reasoning-only reply is an answer half-finished, not a provider empty.

DIAG 2026-09-13 §4.1. `_is_empty_completion` (`llm_client.py:385-402`) read
only `message.content`, so a turn that came back with `reasoning_content` and
a blank `content` — the normal shape of a thinking model that spent its budget
before the answer started — was thrown away at `llm_client.py:1646` and
re-rolled identically. Measured: ~47 of 50 agent-loop "returned no content"
events a day, at a median of 8 SECONDS of generation each (§1.3) — the engine
was paying for the reasoning and then discarding it.

`compaction.py:302-309` had the same defect coded independently, with a worse
consequence: a blank summary walked the whole model chain down to the local
27B tier, which produced a 30-character summary of an 81k-token context
(§1.5). 61 such walks a day.

The fix re-asks the SAME model once with a smaller thinking budget and a
nudge, because an identical re-roll reproduces an identical truncation. A true
empty — no reasoning, no tool call, zero output tokens — keeps today's path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import compaction, llm_client
from robothor.engine.llm_attempts import (
    REASONING_ONLY_NUDGE,
    REASONING_ONLY_RETRY_BUDGET,
    describe_completion,
)
from robothor.engine.llm_client import LLMClient, _is_empty_completion
from robothor.engine.model_breaker import ModelBreaker

THINKING_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"


def _response(
    *,
    content: str | None = "an answer",
    tool_calls: Any = None,
    finish_reason: str = "stop",
    completion_tokens: int = 40,
    reasoning_tokens: int | None = None,
    **reasoning_fields: Any,
) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls, **reasoning_fields)
    usage = SimpleNamespace(
        prompt_tokens=900,
        completion_tokens=completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, model=THINKING_MODEL)


def _reasoning_only(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "content": "",
        "finish_reason": "length",
        "completion_tokens": 9800,
        "reasoning_tokens": 9800,
        "reasoning_content": "let me think about the dedup keys…",
    }
    return _response(**{**defaults, **overrides})


@pytest.fixture(autouse=True)
def _no_jitter_and_isolated_breaker(monkeypatch):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)


# ─── detection ──────────────────────────────────────────────────────────


class TestDetection:
    @pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_details"])
    def test_every_reasoning_field_counts_as_reasoning(self, field: str) -> None:
        """The three fields `reasoning_replay.py:35-39` already defines."""
        shape = describe_completion(_response(content="", **{field: "thought"}))
        assert shape.reasoning_present is True
        assert shape.reasoning_only is True

    def test_a_reasoning_only_turn_still_has_no_answer(self) -> None:
        shape = describe_completion(_reasoning_only())
        assert shape.no_answer is True
        assert shape.reasoning_only is True

    def test_a_true_empty_is_not_reasoning_only(self) -> None:
        shape = describe_completion(_response(content="", completion_tokens=0))
        assert shape.no_answer is True
        assert shape.reasoning_only is False

    def test_reasoning_plus_a_tool_call_is_a_normal_turn(self) -> None:
        """Retrying these would re-issue side-effectful tool calls."""
        shape = describe_completion(
            _response(content="", tool_calls=[{"id": "1"}], reasoning_content="…")
        )
        assert shape.no_answer is False
        assert shape.reasoning_only is False

    def test_the_2026_08_22_guard_is_not_disarmed(self) -> None:
        """Content-blank with no reasoning is still empty."""
        assert _is_empty_completion(_response(content="")) is True
        assert _is_empty_completion(_response(content="hello")) is False


# ─── the agent loop re-asks the same model ──────────────────────────────


@pytest.mark.asyncio
async def test_reasoning_only_retries_the_same_model_with_a_lower_budget() -> None:
    acompletion = AsyncMock(side_effect=[_reasoning_only(), _response()])
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        result = await LLMClient()._call_llm(
            [{"role": "user", "content": "hi"}],
            [THINKING_MODEL, "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert getattr(result.choices[0].message, "content", None) == "an answer"
    assert acompletion.call_count == 2
    called = [c.kwargs["model"] for c in acompletion.call_args_list]
    assert called[0] == called[1] and "deepseek" in called[0], (
        "the chain must not advance on a reasoning-only reply"
    )

    first, second = acompletion.call_args_list
    assert second.kwargs["thinking"]["budget_tokens"] < first.kwargs["thinking"]["budget_tokens"], (
        "an identical re-roll truncates identically"
    )
    assert second.kwargs["thinking"]["budget_tokens"] == REASONING_ONLY_RETRY_BUDGET
    assert REASONING_ONLY_NUDGE in str(second.kwargs["messages"][-1])


@pytest.mark.asyncio
async def test_reasoning_only_is_logged_distinctly_from_empty(caplog) -> None:
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=[_reasoning_only(), _response()]),
        ),
        caplog.at_level("WARNING", logger="robothor.engine.llm_client"),
    ):
        await LLMClient()._call_llm(
            [{"role": "user", "content": "hi"}], [THINKING_MODEL], [], broken_models=set()
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "reasoning_only" in logged
    assert "returned no content and no tool call" not in logged


@pytest.mark.asyncio
async def test_the_re_ask_is_spent_once_not_forever() -> None:
    """Two reasoning-only replies must advance the chain, not loop."""
    acompletion = AsyncMock(
        side_effect=[_reasoning_only(), _reasoning_only(), _reasoning_only(), _response()]
    )
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        result = await LLMClient()._call_llm(
            [{"role": "user", "content": "hi"}],
            [THINKING_MODEL, "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert getattr(result.choices[0].message, "content", None) == "an answer"
    assert acompletion.call_args_list[-1].kwargs["model"].endswith("fallback")


@pytest.mark.asyncio
async def test_a_true_empty_keeps_todays_path() -> None:
    """No reasoning, zero output tokens: still an empty, still re-rolled as one."""
    empty = _response(content="", completion_tokens=0)
    acompletion = AsyncMock(side_effect=[empty, _response()])
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        await LLMClient()._call_llm(
            [{"role": "user", "content": "hi"}], [THINKING_MODEL], [], broken_models=set()
        )
    first, second = acompletion.call_args_list
    assert (
        second.kwargs["thinking"]["budget_tokens"] == first.kwargs["thinking"]["budget_tokens"]
    ), "a provider empty is not a budget problem — do not shrink the budget for it"
    assert REASONING_ONLY_NUDGE not in str(second.kwargs["messages"])


# ─── compaction re-asks instead of walking the chain ────────────────────


@pytest.mark.asyncio
async def test_compaction_accepts_a_reasoning_only_reply_without_walking() -> None:
    """DIAG §1.5: the walk ended at a local 27B and lost the context."""
    calls: list[str] = []

    async def _fake(model: str, **kwargs: Any) -> Any:
        calls.append(model)
        return _reasoning_only() if len(calls) == 1 else _response(content="a summary")

    with patch("robothor.engine.compaction.pooled_acompletion", new=_fake):
        response = await compaction._acompletion_over_chain(
            [THINKING_MODEL, "openrouter/second", "ollama_chat/qwen3.8:27b"],
            messages=[{"role": "user", "content": "summarise"}],
        )
    assert response.choices[0].message.content == "a summary"
    assert calls == [THINKING_MODEL, THINKING_MODEL], "the chain must not be walked"


@pytest.mark.asyncio
async def test_compaction_re_ask_asks_for_less_thinking() -> None:
    seen: list[dict[str, Any]] = []

    async def _fake(model: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return _reasoning_only() if len(seen) == 1 else _response(content="a summary")

    with patch("robothor.engine.compaction.pooled_acompletion", new=_fake):
        await compaction._acompletion_over_chain(
            THINKING_MODEL, messages=[{"role": "user", "content": "summarise"}]
        )
    assert seen[1].get("reasoning_effort") == "low"
    assert REASONING_ONLY_NUDGE in str(seen[1]["messages"][-1])


@pytest.mark.asyncio
async def test_compaction_still_walks_for_a_true_empty() -> None:
    calls: list[str] = []

    async def _fake(model: str, **kwargs: Any) -> Any:
        calls.append(model)
        return (
            _response(content="", completion_tokens=0)
            if model == THINKING_MODEL
            else _response(content="a summary")
        )

    with patch("robothor.engine.compaction.pooled_acompletion", new=_fake):
        response = await compaction._acompletion_over_chain(
            [THINKING_MODEL, "openrouter/second"],
            messages=[{"role": "user", "content": "summarise"}],
        )
    assert response.choices[0].message.content == "a summary"
    assert calls == [THINKING_MODEL, "openrouter/second"]
