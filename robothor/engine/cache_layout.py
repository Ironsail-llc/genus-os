"""Prompt-cache layout v2: keep the request prefix byte-stable between runs.

Gated by ``ROBOTHOR_CACHE_LAYOUT_V2`` (``feature_flags.cache_layout_v2_enabled``)
and applied by ``LLMClient._build_llm_kwargs``.

Every provider that caches prompts caches a prefix. The engine's system prompt
ends with a ``Current time:`` line at minute precision, so with it in place the
first message changes on every run and nothing after it can be reused. This
module:

* moves that line, and the per-run task-context record (run id, request,
  objective) that ``task_context`` keeps in the session's system message, out
  of the system message on the wire into one engine-context turn
  (``ENGINE_CONTEXT_ROLE``) immediately before the latest user message. Never
  into the operator's own text: ``_normalize_developer_role`` labels the turn
  ``[engine]`` for every provider and, on Anthropic and Gemini, rewrites it to
  a user turn (which Anthropic merges into the start of the latest user turn);
  OpenAI receives a native ``developer`` turn;
* decides which routes get explicit ``cache_control`` markers at all. OpenAI
  caches 1024+ token prefixes automatically and Gemini caches implicitly, so
  they get none; Gemini would turn a marker into explicit context caching,
  which it refuses on agent calls;
* places breakpoints for Anthropic direct: the last tool definition, and the
  last user or tool message before the final two turns. The system message
  keeps the breakpoint the builder already gives it, three of the four
  Anthropic allows. litellm (1.100.x) carries a tool-level ``cache_control``
  and a message-level one on user (string content) and tool messages into the
  Anthropic request; list content takes the marker on its last block.
"""

from __future__ import annotations

import json
from typing import Any

_SEPARATOR = "\n\n---\n\n"
_TIME_MARKER = f"{_SEPARATOR}Current time:"
_EPHEMERAL = {"type": "ephemeral"}


def prepare(model: str, messages: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]], bool]:
    """(layout v2 on, messages to send, whether the system message gets a breakpoint)."""
    from robothor.engine.feature_flags import cache_layout_v2_enabled
    from robothor.engine.model_registry import supports_cache_control

    cache_system = supports_cache_control(model)
    if not cache_layout_v2_enabled():
        return False, messages, cache_system
    return True, move_time_to_engine_turn(messages), sends_cache_markers(model, cache_system)


def _take_time_line(content: str) -> tuple[str, str]:
    """(content without the ``Current time:`` line, the line or "")."""
    start = content.rfind(_TIME_MARKER)
    if start < 0:
        return content, ""
    line_start = start + len(_SEPARATOR)
    end = content.find("\n", line_start)
    end = len(content) if end < 0 else end
    return content[:start] + content[end:], content[line_start:end]


def _take_task_record(content: str) -> tuple[str, str]:
    """(content without the per-run task-context record, the record or "").

    ``task_context.install_context`` keeps the record (run id, request,
    objective, steering) in the session's system message so it survives
    compaction and its readers find it there. Only the copy on the wire moves.
    """
    from robothor.engine.task_context import MARKER

    start = content.find(MARKER)
    if start < 0:
        return content, ""
    instruction_end = content.find("\n", start + len(MARKER))
    if instruction_end < 0:
        return content, ""
    try:
        _, end = json.JSONDecoder().raw_decode(content, instruction_end + 1)
    except ValueError:
        return content, ""
    return content[:start] + content[end:], content[start:end].strip()


def move_time_to_engine_turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Take the per-run parts out of the system message.

    The ``Current time:`` line and the task-context record change on every run;
    they are re-sent together as one engine turn before the latest user
    message. A payload with neither, no string system message or no user turn
    is returned untouched (same list object). Copy-on-write: the session keeps
    its own messages.
    """
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    if not messages or messages[0].get("role") != "system":
        return messages
    content = messages[0].get("content")
    if not isinstance(content, str):
        return messages
    last_user = next(
        (i for i in range(len(messages) - 1, 0, -1) if messages[i].get("role") == "user"),
        None,
    )
    if last_user is None:
        return messages
    content, time_line = _take_time_line(content)
    content, record = _take_task_record(content)
    per_run = "\n\n".join(part for part in (time_line, record) if part)
    if not per_run:
        return messages
    system = {**messages[0], "content": content}
    turn = {"role": ENGINE_CONTEXT_ROLE, "content": per_run}
    return [system, *messages[1:last_user], turn, *messages[last_user:]]


def is_openai_family(model: str) -> bool:
    """OpenAI's own API shape: prompt caching is automatic, markers are noise."""
    lowered = model.lower()
    return lowered.startswith(("openai/", "azure/", "gpt-", "chatgpt-"))


def sends_cache_markers(model: str, supports_cache_control: bool) -> bool:
    """Whether the system-message breakpoint is sent under the v2 layout."""
    return supports_cache_control and not is_openai_family(model)


def has_explicit_breakpoints(model: str) -> bool:
    """Anthropic direct: tool and conversation breakpoints, not only the system one."""
    return model.startswith("anthropic/")


def add_anthropic_breakpoints(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mark the last tool and the last user/tool message before the final two turns."""
    if tools:
        tools = [*tools[:-1], {**tools[-1], "cache_control": _EPHEMERAL}]
    for i in range(len(messages) - 3, 0, -1):
        msg = messages[i]
        if msg.get("role") not in {"user", "tool"} or not msg.get("content"):
            continue
        content = msg["content"]
        if isinstance(content, list):
            last = content[-1]
            if not isinstance(last, dict) or last.get("type") != "text":
                continue
            marked = {**msg, "content": [*content[:-1], {**last, "cache_control": _EPHEMERAL}]}
        else:
            marked = {**msg, "cache_control": _EPHEMERAL}
        messages = [*messages[:i], marked, *messages[i + 1 :]]
        break
    return messages, tools
