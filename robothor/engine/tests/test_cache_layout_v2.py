"""Prompt-cache layout v2 (``ROBOTHOR_CACHE_LAYOUT_V2``).

Every provider that caches prompts caches a PREFIX: Anthropic up to an explicit
``cache_control`` breakpoint, OpenAI automatically for prefixes of 1024+
tokens, Gemini implicitly. The system prompt used to end with
``Current time: <weekday, date, HH:MM>``, so the very first message changed
every minute and no provider could reuse anything past it: not the guardrail
summary appended after the time, not one message of the conversation history.

With the flag on the kwargs builder:

* takes the time line out of the system message and sends it as an engine
  context turn immediately before the latest user message, so the system
  message is byte-identical from one run to the next;
* on Anthropic direct, places breakpoints on the last tool definition, the
  system message, and the last user/tool message before the final two turns;
* sends no ``cache_control`` at all to OpenAI-family, Gemini or OpenRouter
  routes, which cache the now-stable prefix on their own.

With the flag off every payload is what it was.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime as _real_datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import config as engine_config
from robothor.engine.llm_client import LLMClient

FLAG = "ROBOTHOR_CACHE_LAYOUT_V2"
ANTHROPIC_DIRECT = "anthropic/claude-sonnet-4-6"
VERTEX_GEMINI = "vertex_ai/gemini-2.5-pro"
GEMINI = "gemini/gemini-2.5-flash"
OPENAI = "openai/gpt-5.1"
OPENROUTER = "openrouter/anthropic/claude-sonnet-4-6"
NO_MARKER_MODELS = [VERTEX_GEMINI, GEMINI, OPENAI, OPENROUTER]
ALL_MODELS = [ANTHROPIC_DIRECT, *NO_MARKER_MODELS]

STATIC_BODY = "You are example-agent.\n\n---\n\n" + ("Rule: be precise and brief. " * 300)
GUARDRAIL = "## Guardrails\nThe following policies are enforced: no_destructive_writes."

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": f"tool_{i}",
            "description": f"Example tool number {i}.",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
    for i in range(3)
]


def _limits() -> Any:
    m = MagicMock()
    m.max_input_tokens = 200_000
    m.cache_write_cost_per_token = 0.0
    m.cache_read_cost_per_token = 0.0
    m.supports_thinking = False
    m.default_output_tokens = 16_384
    m.max_output_tokens = 64_000
    return m


def _system_at(hour: int, minute: int) -> str:
    """The system prompt the engine builds at a given wall-clock minute."""

    class _Frozen(_real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _real_datetime(2026, 3, 4, hour, minute, tzinfo=tz)

    with patch.object(engine_config, "datetime", _Frozen):
        parts = engine_config._prompt_with_time(STATIC_BODY, "UTC")
    # request_runtime.prepare_loop appends the guardrail summary after the tail.
    return parts.full_text() + f"\n\n---\n\n{GUARDRAIL}"


def _history(n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"Turn {i}: " + ("lorem ipsum dolor " * 25)})
    return out


def _messages(system: str, history: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]


def _build(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None):
    with (
        patch("robothor.engine.model_registry.get_model_limits", return_value=_limits()),
        patch("robothor.engine.model_registry.get_output_tokens", return_value=4096),
    ):
        return LLMClient._build_llm_kwargs(
            model, messages, TOOLS if tools is None else tools, input_est=1000, temperature=0.3
        )


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "1")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


# ─── The system message is the stable prefix ─────────────────────────────


@pytest.mark.parametrize("model", ALL_MODELS)
def test_system_message_byte_identical_a_minute_apart_with_flag_on(flag_on, model):
    first = _build(model, _messages(_system_at(9, 41), _history(4), "hello"))
    second = _build(model, _messages(_system_at(9, 42), _history(4), "hello"))
    assert _dump(first["messages"][0]) == _dump(second["messages"][0])


@pytest.mark.parametrize("model", ALL_MODELS)
def test_system_message_differs_a_minute_apart_with_flag_off(flag_off, model):
    first = _build(model, _messages(_system_at(9, 41), _history(4), "hello"))
    second = _build(model, _messages(_system_at(9, 42), _history(4), "hello"))
    assert _dump(first["messages"][0]) != _dump(second["messages"][0])


@pytest.mark.parametrize("model", ALL_MODELS)
def test_guardrail_summary_stays_in_the_system_prefix(flag_on, model):
    kwargs = _build(model, _messages(_system_at(9, 41), _history(2), "hello"))
    assert GUARDRAIL in _dump(kwargs["messages"][0])
    assert "Current time:" not in _dump(kwargs["messages"][0])


@pytest.mark.parametrize("model", ALL_MODELS)
def test_time_goes_in_an_engine_turn_just_before_the_latest_user_message(flag_on, model):
    kwargs = _build(model, _messages(_system_at(9, 41), _history(4), "what next?"))
    msgs = kwargs["messages"]
    holders = [i for i, m in enumerate(msgs) if "Current time:" in _dump(m)]
    assert holders == [len(msgs) - 2]
    assert "09:41 AM" in _dump(msgs[-2])
    assert msgs[-2]["role"] in {"developer", "user"}
    # The operator's own text is never edited.
    assert msgs[-1] == {"role": "user", "content": "what next?"}


def test_time_turn_is_engine_labelled_on_anthropic(flag_on):
    kwargs = _build(ANTHROPIC_DIRECT, _messages(_system_at(9, 41), _history(2), "hi"))
    turn = kwargs["messages"][-2]
    assert turn["role"] == "user"  # Anthropic hoists developer turns; it is rewritten
    assert _dump(turn).count("[engine]") == 1


def test_openai_gets_a_native_developer_turn(flag_on):
    kwargs = _build(OPENAI, _messages(_system_at(9, 41), _history(2), "hi"))
    assert kwargs["messages"][-2]["role"] == "developer"


def test_session_messages_are_not_mutated(flag_on):
    messages = _messages(_system_at(9, 41), _history(6), "hi")
    tools = copy.deepcopy(TOOLS)
    before_msgs, before_tools = copy.deepcopy(messages), copy.deepcopy(tools)
    _build(ANTHROPIC_DIRECT, messages, tools)
    assert messages == before_msgs
    assert tools == before_tools


# ─── Breakpoints ──────────────────────────────────────────────────────────


def test_anthropic_direct_breakpoints_after_tools_system_and_before_last_two(flag_on):
    history = _history(40)
    kwargs = _build(ANTHROPIC_DIRECT, _messages(_system_at(9, 41), history, "next?"))
    msgs = kwargs["messages"]

    tools = kwargs["tools"]
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}
    assert all("cache_control" not in _dump(t) for t in tools[:-1])

    assert "cache_control" in _dump(msgs[0])

    marked = [i for i, m in enumerate(msgs) if i > 0 and "cache_control" in _dump(m)]
    assert len(marked) == 1
    idx = marked[0]
    assert idx <= len(msgs) - 3
    assert msgs[idx]["role"] in {"user", "tool"}
    # The nearest eligible message before the final two turns.
    assert all(msgs[j]["role"] not in {"user", "tool"} for j in range(idx + 1, len(msgs) - 2))

    assert _dump(kwargs).count("cache_control") == 3  # Anthropic allows at most 4


def test_anthropic_breakpoint_lands_on_a_tool_result(flag_on):
    messages = [
        {"role": "system", "content": _system_at(9, 41)},
        {"role": "user", "content": "look it up"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool_0", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result " * 50},
        {"role": "assistant", "content": "Found it."},
        {"role": "user", "content": "thanks"},
    ]
    kwargs = _build(ANTHROPIC_DIRECT, messages)
    msgs = kwargs["messages"]
    marked = [m for m in msgs[1:] if "cache_control" in _dump(m)]
    assert len(marked) == 1
    assert marked[0]["role"] == "tool"
    assert marked[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("model", NO_MARKER_MODELS)
def test_no_cache_control_sent_to_implicit_cache_routes(flag_on, model):
    kwargs = _build(model, _messages(_system_at(9, 41), _history(40), "next?"))
    assert "cache_control" not in _dump(kwargs)


def test_flag_off_anthropic_tools_and_history_carry_no_breakpoint(flag_off):
    kwargs = _build(ANTHROPIC_DIRECT, _messages(_system_at(9, 41), _history(40), "next?"))
    assert "cache_control" not in _dump(kwargs["tools"])
    assert all("cache_control" not in _dump(m) for m in kwargs["messages"][1:])


# ─── Measurement: shared prefix across two consecutive chat turns ─────────


def _est_tokens(obj: Any) -> int:
    return max(1, len(_dump(obj)) // 4)


def shared_prefix_tokens(model: str) -> tuple[int, int]:
    """(tokens shared with the previous turn's request, tokens in this request).

    Turn N has a 40-message history; turn N+1 is the same conversation one
    exchange later, its system prompt built a minute later. Counted in the
    order providers hash: tools, then messages, stopping at the first
    difference.
    """
    history = _history(40)
    turn_n = _build(model, _messages(_system_at(9, 41), history, "question one"))
    later = [*history, {"role": "user", "content": "question one"}]
    later.append({"role": "assistant", "content": "Answer one."})
    turn_n1 = _build(model, _messages(_system_at(9, 42), later, "question two"))

    def _strip(obj: Any) -> Any:
        # A breakpoint marker is not content: providers hash the content.
        if isinstance(obj, dict):
            return {k: _strip(v) for k, v in obj.items() if k != "cache_control"}
        if isinstance(obj, list):
            return [_strip(v) for v in obj]
        return obj

    a = [_strip(turn_n["tools"]), *(_strip(m) for m in turn_n["messages"])]
    b = [_strip(turn_n1["tools"]), *(_strip(m) for m in turn_n1["messages"])]
    shared = 0
    for x, y in zip(a, b, strict=False):
        if _dump(x) != _dump(y):
            break
        shared += _est_tokens(x)
    return shared, sum(_est_tokens(x) for x in b)


@pytest.mark.parametrize("model", ALL_MODELS)
def test_forty_message_history_shares_at_least_ninety_percent_with_flag_on(flag_on, model):
    shared, total = shared_prefix_tokens(model)
    assert shared / total >= 0.9


@pytest.mark.parametrize("model", ALL_MODELS)
def test_forty_message_history_shares_only_the_tools_with_flag_off(flag_off, model):
    shared, total = shared_prefix_tokens(model)
    assert shared / total < 0.1
