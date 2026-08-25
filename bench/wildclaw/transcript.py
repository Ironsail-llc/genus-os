"""Genus conversation → WildClawBench transcript.

WildClawBench's graders walk a transcript of Anthropic-shaped message blocks
(the format OpenClaw writes to `chat.jsonl`) and accept one passed in as
``kwargs["transcript"]``. Genus keeps its conversation in OpenAI shape. This
is the bridge.

It is deliberately the smallest possible piece of the harness, pure and
separately tested, because it is the part where a mistake becomes a published
number that is not true: a converter that drops a tool call makes Genus look
safer than it is, and one that mangles an argument makes it look worse.
"""

from __future__ import annotations

import json
from typing import Any

#: The system prompt is Genus's own scaffolding, not conversation. No other
#: harness's transcript contains its instructions, so publishing ours would
#: hand every text-scanning grader extra material — an unfair diff in our
#: favour, and an incomparable one.
_EXCLUDED_ROLES = frozenset({"system"})


def _tool_call_parts(call: Any) -> tuple[str, str, str]:
    """(id, name, raw_arguments) from either a litellm object or a plain dict."""
    if isinstance(call, dict):
        fn = call.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        return (
            str(call.get("id") or ""),
            str(fn.get("name") or call.get("name") or ""),
            str(fn.get("arguments") if fn.get("arguments") is not None else ""),
        )
    fn = getattr(call, "function", None)
    return (
        str(getattr(call, "id", "") or ""),
        str(getattr(fn, "name", "") or ""),
        str(getattr(fn, "arguments", "") or ""),
    )


def _decode_arguments(raw: str) -> Any:
    """Parsed JSON when possible, otherwise the raw string.

    A model can emit malformed JSON. Dropping the call would hide a command
    the grader must see; WildClawBench's own ``_extract_command_text`` takes a
    bare string, so hand it through rather than losing it.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Multimodal turns already carry blocks; keep only what a text grader
        # can read, and never invent a shape for image parts.
        blocks.extend(
            {"type": "text", "text": str(part.get("text", ""))}
            for part in text
            if isinstance(part, dict) and part.get("type") == "text"
        )

    for call in message.get("tool_calls") or []:
        call_id, name, raw_args = _tool_call_parts(call)
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": _decode_arguments(raw_args),
            }
        )
    return blocks


def to_wildclaw_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert an OpenAI-shaped conversation into WildClawBench entries.

    Assistant turns become block lists (text first, then each tool call, in
    the order the model produced them). Other roles are carried through as-is:
    the safety graders read assistant turns only, but a record that silently
    drops the prompt and the tool results is not a transcript, and other
    tasks' graders do read them.
    """
    entries: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role in _EXCLUDED_ROLES:
            continue

        if role == "assistant":
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": _assistant_content(message),
            }
        else:
            payload = {"role": role, "content": message.get("content", "")}
            if message.get("tool_call_id"):
                payload["tool_call_id"] = message["tool_call_id"]

        entries.append({"type": "message", "message": payload})
    return entries


def steps_to_transcript(
    steps: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build a transcript from the PERSISTED steps, plus the session's prose.

    `session.messages` is the window the model is still carrying, not the
    whole run. Measured on one Productivity Flow task: `agent_run_steps` held
    174 tool calls and the message list held 62 — same run, token totals
    matching to within 0.3%. Long runs shed most of their history, which is
    right for the model and wrong for a record of what happened.

    The graders read this to decide what the agent DID — which commands ran,
    whether a credential store was touched, whether the API it was asked to
    call was ever called. A transcript missing two thirds of the tool calls
    understates the agent in both directions: it hides unsafe actions exactly
    as readily as completed work.

    So tool calls come from the steps, which are complete by construction, and
    the prose comes from the messages, because steps record what was done and
    never what was said. Non-assistant turns are carried through from the
    session for the same reason.
    """
    entries: list[dict[str, Any]] = []

    # The prompt and any non-assistant context, in the order the session has.
    for message in messages:
        role = str(message.get("role") or "")
        if role in _EXCLUDED_ROLES or role == "assistant":
            continue
        if role == "tool":
            continue  # results are emitted with their call, below
        entries.append(
            {"type": "message", "message": {"role": role, "content": message.get("content", "")}}
        )

    # Assistant prose, which no step records.
    for message in messages:
        if str(message.get("role") or "") != "assistant":
            continue
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            entries.append(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )

    # Every tool call, from the authoritative log, each with its result.
    for step in sorted(steps, key=lambda s: s.get("step_number") or 0):
        if str(step.get("step_type") or "") != "tool_call":
            continue
        call_id = f"step-{step.get('step_number')}"
        entries.append(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": str(step.get("tool_name") or ""),
                            "input": step.get("tool_input") or {},
                        }
                    ],
                },
            }
        )
        entries.append(
            {
                "type": "message",
                "message": {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": step.get("tool_output"),
                },
            }
        )
    return entries
