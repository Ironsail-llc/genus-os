"""Engine context is attributed to the engine for every provider.

Live, 2026-09-14 (main agent on DeepSeek via OpenRouter): every reply to the
operator opened with a "security flag" that his message "arrived wrapped in
injected blocks — a --- CURRENT USER --- header, an [EXECUTION PLAN], and a
[WORKING STATE] block", and that the agent had "ignored the injected
instructions". Those blocks are the engine's own plan, working state and
identity context, sent as unattributed ``developer`` turns beside the user's
text; the provider folds them next to the user message, and behavioural rule
6 tells the agent to flag anything that looks injected. Attribution existed —
but only on the Anthropic path. Every provider now gets it, and the rules say
what the prefix means.
"""

from __future__ import annotations

from robothor.engine.llm_client import ENGINE_CONTEXT_PREFIX, LLMClient
from robothor.engine.prompts import BEHAVIORAL_RULES
from robothor.engine.session import ENGINE_CONTEXT_ROLE


def _msgs(content: str = "[EXECUTION PLAN]\nDifficulty: easy") -> list[dict]:
    return [
        {"role": "system", "content": "You are main."},
        {"role": "user", "content": "what's waiting on me?"},
        {"role": ENGINE_CONTEXT_ROLE, "content": content},
    ]


def test_a_non_anthropic_model_still_gets_the_engine_prefix() -> None:
    out = LLMClient._normalize_developer_role("openrouter/deepseek/deepseek-v4.1-flash", _msgs())
    assert out[-1]["content"].startswith(ENGINE_CONTEXT_PREFIX)
    assert out[-1]["role"] == ENGINE_CONTEXT_ROLE, (
        "the role stays developer where the provider accepts it"
    )


def test_an_anthropic_model_gets_the_prefix_and_a_user_turn() -> None:
    out = LLMClient._normalize_developer_role("openrouter/anthropic/claude-sonnet-4.6", _msgs())
    assert out[-1]["role"] == "user"
    assert out[-1]["content"].startswith(ENGINE_CONTEXT_PREFIX)


def test_the_prefix_is_never_doubled() -> None:
    once = LLMClient._normalize_developer_role("openrouter/deepseek/deepseek-v4.1-flash", _msgs())
    twice = LLMClient._normalize_developer_role("openrouter/deepseek/deepseek-v4.1-flash", once)
    assert twice[-1]["content"].count(ENGINE_CONTEXT_PREFIX.strip()) == 1


def test_user_and_assistant_turns_are_untouched() -> None:
    msgs = _msgs()
    out = LLMClient._normalize_developer_role("openrouter/deepseek/deepseek-v4.1-flash", msgs)
    assert out[0] == msgs[0] and out[1] == msgs[1]


def test_list_content_gets_a_leading_text_block() -> None:
    msgs = [
        {"role": ENGINE_CONTEXT_ROLE, "content": [{"type": "text", "text": "[WORKING STATE] x"}]}
    ]
    out = LLMClient._normalize_developer_role("openrouter/deepseek/deepseek-v4.1-flash", msgs)
    assert out[0]["content"][0] == {"type": "text", "text": ENGINE_CONTEXT_PREFIX.strip()}


def test_the_rules_say_what_the_prefix_means() -> None:
    assert ENGINE_CONTEXT_PREFIX.strip() in BEHAVIORAL_RULES
    lowered = BEHAVIORAL_RULES.lower()
    assert "not injection" in lowered or "never call them injected" in lowered
    assert "execution plan" in lowered and "working state" in lowered
