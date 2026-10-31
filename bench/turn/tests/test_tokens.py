"""The first LLM call's prompt size, estimated at four characters a token."""

from __future__ import annotations

import json

from bench.turn.tokens import estimate_prompt_tokens


def test_messages_and_tool_schemas_count_together() -> None:
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    tool_chars = len(json.dumps(tools))
    messages = [
        {"role": "system", "content": "s" * 1000},
        {"role": "user", "content": "u" * (3000 - tool_chars)},
    ]
    assert estimate_prompt_tokens(messages, tools) == 1000


def test_multipart_content_counts_its_text_parts() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "x" * 40}]}]
    assert estimate_prompt_tokens(messages, None) == 10


def test_tool_call_arguments_in_history_count() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "noop", "arguments": "a" * 36}}],
        }
    ]
    # 36 characters of arguments plus the 4-character name.
    assert estimate_prompt_tokens(messages, None) == 10


def test_nothing_is_zero() -> None:
    assert estimate_prompt_tokens([], None) == 0
