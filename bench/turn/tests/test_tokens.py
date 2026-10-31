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


def _system(when: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "You are example-agent.\n\n---\n\n"
            f"Current time: {when} (UTC offset: -04:00)",
        }
    ]


def test_the_wall_clock_tail_does_not_move_the_estimate() -> None:
    # The runner appends today's date to every system prompt; its length
    # changes with the weekday and month names, the code does not.
    short = estimate_prompt_tokens(_system("Monday, May 04, 2026 09:05 AM EDT"), None)
    long = estimate_prompt_tokens(_system("Wednesday, September 30, 2026 11:59 PM EDT"), None)
    assert short == long


def test_text_around_the_clock_still_counts() -> None:
    with_more = [{**_system("Friday, October 30, 2026 10:00 AM EDT")[0]}]
    with_more[0]["content"] = "x" * 400 + with_more[0]["content"]
    assert (
        estimate_prompt_tokens(with_more, None)
        == estimate_prompt_tokens(_system("Friday, October 30, 2026 10:00 AM EDT"), None) + 100
    )
