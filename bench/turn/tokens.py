"""Prompt size of an LLM call, estimated without a tokenizer.

Four characters a token is the usual rough figure for English and JSON. The
point is a stable number to compare before and after a change (tool deferral,
a shorter preamble), not an exact count for any one model.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

CHARS_PER_TOKEN = 4

#: The runner appends the wall clock to every system prompt
#: (``robothor/engine/config.py``, ``_prompt_with_time``). Its length moves with
#: the weekday and month names (Monday, May vs Wednesday, September), so it is
#: counted as one fixed-length stand-in: the estimate depends on the code, not
#: the date.
_CLOCK = re.compile(r"Current time: [^\n]*?\(UTC offset: [+-]\d{2}:\d{2}\)")
_CLOCK_STAND_IN = "Current time: <now>"


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(_CLOCK.sub(_CLOCK_STAND_IN, content))
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                total += len(_CLOCK.sub(_CLOCK_STAND_IN, str(part.get("text") or "")))
            else:
                total += len(str(part))
        return total
    return len(str(content))


def prompt_chars(messages: Sequence[dict[str, Any]], tools: Any) -> int:
    total = 0
    for message in messages or ():
        total += _content_chars(message.get("content"))
        for call in message.get("tool_calls") or ():
            function = (call or {}).get("function") or {}
            total += len(str(function.get("name") or ""))
            total += len(str(function.get("arguments") or ""))
    if tools:
        total += len(json.dumps(tools, default=str))
    return total


def estimate_prompt_tokens(messages: Sequence[dict[str, Any]], tools: Any) -> int:
    return math.ceil(prompt_chars(messages, tools) / CHARS_PER_TOKEN)
