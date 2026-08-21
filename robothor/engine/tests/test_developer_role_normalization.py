"""Per-model normalization of engine ``developer``-role turns.

Incident (2026-08-21 07:09:13, agent ``main``): the last-resort fallback leg
died with::

    Model openrouter/anthropic/claude-sonnet-4.6 failed: This model does not
    support assistant message prefill. The conversation must end with a user
    message.
    -> All models failed to respond

Mechanism: the engine appends context with ``role="developer"``
(``ENGINE_CONTEXT_ROLE``, robothor/engine/session.py:53) at the conversation
tail — e.g. the verification-retry feedback at robothor/engine/runner.py:3735.
litellm maps ``developer`` -> ``system`` for non-OpenAI providers
(``map_developer_role_to_system_role``) and the Anthropic transformation then
*hoists* every system turn out of the message list into the top-level ``system``
param (``AnthropicConfig.translate_system_message``). What Anthropic actually
receives therefore ends with the ASSISTANT turn that preceded the developer
message, which it rejects as a prefill.

``_guard_trailing_assistant`` could not see this: at guard time the list still
ends with the ``developer`` turn. MiMo tolerates the developer role, so only the
Anthropic last-resort leg died — precisely when it was needed most.

Fix: for anthropic-family models only, rewrite ``developer`` turns as prefixed
user turns *before* the prefill guard runs. Non-anthropic payloads must stay
byte-identical.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.llm_client import LLMClient
from robothor.engine.session import ENGINE_CONTEXT_ROLE

ANTHROPIC_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
MIMO_MODEL = "openrouter/xiaomi/mimo-v2.5"


def _limits() -> MagicMock:
    m = MagicMock()
    m.max_input_tokens = 200_000
    m.cache_write_cost_per_token = 0.0
    m.cache_read_cost_per_token = 0.0
    m.supports_thinking = False
    return m


def _build(model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Call the real kwargs builder with the model registry stubbed out."""
    with (
        patch(
            "robothor.engine.model_registry.get_model_limits",
            return_value=_limits(),
        ),
        patch("robothor.engine.model_registry.get_output_tokens", return_value=4096),
    ):
        return LLMClient._build_llm_kwargs(model, messages, [], input_est=1000, temperature=0.3)


def _tail_conversation() -> list[dict[str, Any]]:
    """user -> assistant -> engine developer context (the failing shape)."""
    return [
        {"role": "user", "content": "summarize the inbox"},
        {"role": "assistant", "content": "partial answer"},
        {"role": ENGINE_CONTEXT_ROLE, "content": "Verification failed: cite sources."},
    ]


class TestAnthropicDeveloperNormalization:
    def test_developer_tail_becomes_user_turn(self) -> None:
        """(a) Anthropic-family: final role is ``user``, content preserved."""
        kwargs = _build(ANTHROPIC_MODEL, _tail_conversation())
        msgs = kwargs["messages"]

        assert msgs[-1]["role"] == "user"
        assert "Verification failed: cite sources." in msgs[-1]["content"]
        assert msgs[-1]["content"].startswith("[engine]")
        # The assistant turn must survive — we normalize, we do not truncate.
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]

    def test_no_developer_role_reaches_anthropic(self) -> None:
        kwargs = _build(ANTHROPIC_MODEL, _tail_conversation())
        assert all(m["role"] != ENGINE_CONTEXT_ROLE for m in kwargs["messages"])

    def test_mid_conversation_developer_also_normalized(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": ENGINE_CONTEXT_ROLE, "content": "budget: 3 steps left"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "go on"},
        ]
        kwargs = _build(ANTHROPIC_MODEL, messages)
        assert [m["role"] for m in kwargs["messages"]] == [
            "user",
            "user",
            "assistant",
            "user",
        ]
        assert kwargs["messages"][1]["content"] == "[engine] budget: 3 steps left"

    def test_bare_claude_model_string_matches(self) -> None:
        """Match is on the model string, not only the ``anthropic/`` prefix."""
        kwargs = _build("claude-sonnet-4-6", _tail_conversation())
        assert kwargs["messages"][-1]["role"] == "user"

    def test_anthropic_direct_model_string_matches(self) -> None:
        kwargs = _build("anthropic/claude-opus-4-8", _tail_conversation())
        assert kwargs["messages"][-1]["role"] == "user"

    def test_input_list_not_mutated(self) -> None:
        messages = _tail_conversation()
        before = copy.deepcopy(messages)
        _build(ANTHROPIC_MODEL, messages)
        assert messages == before

    def test_list_content_developer_turn_is_prefixed(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "partial"},
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": [{"type": "text", "text": "retry with sources"}],
            },
        ]
        kwargs = _build(ANTHROPIC_MODEL, messages)
        last = kwargs["messages"][-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert last["content"][0]["text"].startswith("[engine]")
        assert any("retry with sources" in b["text"] for b in last["content"])


class TestNonAnthropicUnchanged:
    def test_mimo_payload_is_byte_identical(self) -> None:
        """(b) MiMo keeps the developer role — payload must not change."""
        messages = _tail_conversation()
        expected = copy.deepcopy(messages)
        kwargs = _build(MIMO_MODEL, messages)
        assert kwargs["messages"] == expected
        assert kwargs["messages"][-1]["role"] == ENGINE_CONTEXT_ROLE

    def test_mimo_list_object_passed_through_untouched(self) -> None:
        """No defensive copy for the non-anthropic path — same list object."""
        messages = _tail_conversation()
        kwargs = _build(MIMO_MODEL, messages)
        assert kwargs["messages"] is messages

    def test_deepseek_payload_is_byte_identical(self) -> None:
        messages = _tail_conversation()
        expected = copy.deepcopy(messages)
        kwargs = _build("openrouter/deepseek/deepseek-chat", messages)
        assert kwargs["messages"] == expected


class TestRepeatedTrailingAssistants:
    def test_guard_strips_every_trailing_assistant(self) -> None:
        """(c) One pass must leave a legitimately-terminated conversation."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
            {"role": "assistant", "content": "a3"},
        ]
        guarded = LLMClient._guard_trailing_assistant(messages)
        assert [m["role"] for m in guarded] == ["user"]

    def test_guard_stops_at_tool_turn(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        ]
        assert LLMClient._guard_trailing_assistant(messages) is messages

    def test_guard_noop_returns_same_object(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        assert LLMClient._guard_trailing_assistant(messages) is messages

    def test_build_kwargs_strips_repeated_trailing_assistants(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        kwargs = _build(ANTHROPIC_MODEL, messages)
        assert [m["role"] for m in kwargs["messages"]] == ["user"]


class TestVerificationRetryRegression:
    """(d) Regression for the 2026-08-21 07:09 ``main`` fallback-chain death."""

    def test_verification_retry_feedback_does_not_strand_assistant_tail(self) -> None:
        # Exactly the shape runner.py:3735 produces after "Verification failed,
        # retrying once": tool loop, final assistant answer, developer feedback.
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "draft the update"},
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            {"role": "assistant", "content": "here is the draft"},
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": "Verification failed. Fix: add the missing dates.",
            },
        ]
        kwargs = _build(ANTHROPIC_MODEL, messages)
        sent = kwargs["messages"]

        # After litellm hoists system turns, the last non-system turn is what
        # Anthropic sees. It must not be an assistant prefill.
        non_system = [m for m in sent if m["role"] not in ("system", ENGINE_CONTEXT_ROLE)]
        assert non_system[-1]["role"] == "user"
        assert "add the missing dates" in non_system[-1]["content"]
        # The assistant answer under review is still in context for the retry.
        assert any(m.get("content") == "here is the draft" for m in sent)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openrouter/anthropic/claude-sonnet-4.6", True),
        ("anthropic/claude-opus-4-8", True),
        ("claude-3-5-haiku", True),
        ("bedrock/anthropic.claude-v2", True),
        ("openrouter/xiaomi/mimo-v2.5", False),
        ("openrouter/deepseek/deepseek-chat", False),
        ("ollama_chat/llama3", False),
        ("codex/gpt-5.5", False),
    ],
)
def test_is_anthropic_family(model: str, expected: bool) -> None:
    assert LLMClient._is_anthropic_family(model) is expected
