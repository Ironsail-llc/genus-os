"""A scripted model for the workspaces dry run: deterministic, offline, free.

The engine reaches every model through ``litellm.acompletion``; the tests patch
that one seam, and so does this. Each call is answered from the conversation
alone, with litellm's own ``mock_response`` / ``mock_tool_calls``, so the engine
gets real litellm objects (streamed or not) and runs its whole tool loop.

The script, by the latest turn:

* a tool result: the next step for that tool (figures -> publish an edition;
  anything else -> say what happened, quoting the link when there is one);
* "create a daily sales report": restate what will be made and ask;
* "yes, create it": ``room_create`` (a scheduled report, shared with Bob);
* "preview": ``room_run`` with ``preview: true`` on the workspace named;
* "what's new": ``room_list``;
* a workspace's run prompt (its instructions say "Summarise the sales"):
  ``sample_sales_figures`` then ``room_publish_edition``;
* "email the lapsed buyers": ``room_propose_email`` to two fixture contacts,
  with no workspace id (the turn's own workspace is meant);
* a run prompt whose instructions say "then start another run": ``room_run``
  on its own workspace, which an unattended run must be refused;
* "(take your time)" anywhere: the answer waits ``SLOW_SECONDS`` first, so a
  second sender meets a busy workspace;
* anything else: "Answering {name}: {text}", so attribution is visible.

A tool the engine did not offer is never called: the answer says so instead,
so a missing grant shows up in the dry run rather than as a silent no.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any


def _trace(message: str, *args: Any) -> None:
    """One line per model call on stderr (the engine's log), for debugging a run."""
    print("rooms-stack " + (message % args), file=sys.stderr, flush=True)


#: How long a "(take your time)" turn holds the workspace.
SLOW_SECONDS = 6.0

#: Who wrote a room turn: the engine prefixes it ``[Name] ``.
_SPEAKER = re.compile(r"^\[(?P<name>[^\]]{1,80})\]\s*(?P<text>.*)$", re.DOTALL)
_ROOM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: Who "the lapsed buyers" are, in the dry run's email step.
LAPSED_BUYERS = ("buyer.one@example.org", "buyer.two@example.org")

#: What the dry run's report is.
REPORT = {
    "kind": "report",
    "title": "Daily sales",
    "instructions": "Summarise the sales figures for the period with sample_sales_figures.",
    "schedule": "0 7 * * *",
    "tz": "UTC",
    "default_grain": "day",
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _offered(tools: list[dict[str, Any]] | None) -> set[str]:
    names = set()
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
    return names


def _call(name: str, args: dict[str, Any], n: int) -> dict[str, Any]:
    return {
        "id": f"call_{name}_{n}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _last_tool(messages: list[dict[str, Any]]) -> tuple[str, Any] | None:
    """The newest tool result since the last user turn, and its tool. The
    engine may add its own developer or system notes after a result."""
    result = None
    for message in reversed(messages):
        role = message.get("role")
        if role == "tool":
            result = message
            break
        if role in ("user", "assistant"):
            return None
    if result is None:
        return None
    call_id = result.get("tool_call_id")
    for message in reversed(messages):
        for call in message.get("tool_calls") or []:
            if call.get("id") == call_id:
                try:
                    data = json.loads(_text(result.get("content")))
                except (TypeError, ValueError):
                    data = _text(result.get("content"))
                return str(call["function"]["name"]), data
    return None


def _last_user(messages: list[dict[str, Any]]) -> tuple[str, str]:
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _text(message.get("content")).strip()
            match = _SPEAKER.match(text)
            if match:
                return match.group("name"), match.group("text").strip()
            return "", text
    return "", ""


def _figures_blocks(figures: dict[str, Any]) -> list[dict[str, Any]]:
    days = figures.get("days") or []
    return [
        {
            "component": "render_metric_grid",
            "props": {
                "metrics": [
                    {"title": "Revenue", "value": figures.get("revenue_total", 0)},
                    {"title": "Orders", "value": figures.get("orders_total", 0)},
                ]
            },
        },
        {
            "component": "render_bar_chart",
            "props": {
                "title": "Revenue by day",
                "dataKey": "value",
                "nameKey": "name",
                "data": [{"name": d["date"], "value": d["revenue"]} for d in days],
            },
        },
        {"component": "render_markdown", "props": {"content": figures.get("note", "")}},
    ]


def decide(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """``{"text": ...}`` or ``{"tool_calls": [...]}`` for this turn."""
    offered = _offered(tools)
    n = len(messages)

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in offered:
            return {"text": f"I cannot use {name} here (it was not offered to me)."}
        return {"tool_calls": [_call(name, args, n)]}

    after = _last_tool(messages)
    if after is not None:
        name, data = after
        if name == "sample_sales_figures" and isinstance(data, dict):
            # The run prompt names the workspace to publish into ("room_id ...").
            room = re.search(r"room_id ([0-9a-f-]{36})", _last_user(messages)[1])
            return call(
                "room_publish_edition",
                {
                    "room_id": room.group(1) if room else "",
                    "title": f"Sales {data.get('period', '')}".strip(),
                    "summary": data.get("note", ""),
                    "blocks": _figures_blocks(data),
                    "suggestions": [
                        {
                            "key": "follow-up-lapsed",
                            "title": "Call the lapsed buyers",
                            "detail": "Three buyers have not ordered this month.",
                            "action_hint": "crm",
                        }
                    ],
                },
            )
        link = data.get("link") if isinstance(data, dict) else None
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            return {"text": f"{name} was refused: {error}"}
        if name == "room_list" and isinstance(data, dict):
            rooms = data.get("rooms") or []
            lines = [
                f"- {r.get('title')} ({r.get('unread', 0)} new): {r.get('link')}" for r in rooms
            ]
            return {"text": "Your workspaces:\n" + "\n".join(lines) if lines else "You have none."}
        return {"text": f"Done ({name}).{f' Open it: {link}' if link else ''}"}

    name, text = _last_user(messages)
    lowered = text.lower()
    if "create a daily sales report" in lowered:
        return {
            "text": (
                "I will create a report workspace “Daily sales”: it covers yesterday, runs "
                "every day at 07:00 UTC, reads sample_sales_figures, and is shared with Bob. "
                "Reply “yes, create it” and I will."
            )
        }
    if lowered.startswith("yes, create it"):
        share = re.findall(r"share with ([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", text)[:1]
        args = dict(REPORT)
        if share:
            args["share_with"] = share
        return call("room_create", args)
    if "preview" in lowered:
        room = _ROOM_ID.search(text)
        return call("room_run", {"room_id": room.group(0) if room else "", "preview": True})
    if "what's new" in lowered or "whats new" in lowered:
        return call("room_list", {})
    if "email the lapsed buyers" in lowered:
        # Proposes only, for the turn's own workspace (no id: the turn is bound to it).
        return call(
            "room_propose_email",
            {
                "to": list(LAPSED_BUYERS),
                "subject": "Checking in",
                "body": "Hello,\n\nWe have not seen an order from you this month. "
                "Is there anything we can help with?\n\nBest regards",
            },
        )
    if "then start another run" in lowered:
        # A run trying to start more runs: it must be refused (unattended).
        room = re.search(r"room_id ([0-9a-f-]{36})", text)
        return call("room_run", {"room_id": room.group(1) if room else ""})
    if "summarise the sales" in lowered:
        return call("sample_sales_figures", {})
    who = name or "you"
    return {"text": f"Answering {who}: {text}"}


class _Stream:
    """An async stream of litellm chunks: the tool calls, then the finish."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _streamed_tool_calls(calls: list[dict[str, Any]], model: str) -> _Stream:
    from litellm.types.utils import (
        ChatCompletionDeltaToolCall,
        Delta,
        Function,
        ModelResponseStream,
        StreamingChoices,
    )

    deltas = [
        ChatCompletionDeltaToolCall(
            id=call["id"],
            type="function",
            index=i,
            function=Function(
                name=call["function"]["name"], arguments=call["function"]["arguments"]
            ),
        )
        for i, call in enumerate(calls)
    ]
    first = ModelResponseStream(
        model=model,
        choices=[StreamingChoices(index=0, delta=Delta(role="assistant", tool_calls=deltas))],
    )
    last = ModelResponseStream(
        model=model,
        choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="tool_calls")],
    )
    return _Stream([first, last])


def install() -> None:
    """Answer every ``litellm.acompletion`` call from the script."""
    import litellm

    real = litellm.acompletion

    async def scripted(*args: Any, **kwargs: Any) -> Any:
        messages = list(kwargs.get("messages") or (args[1] if len(args) > 1 else []))
        answer = decide(messages, kwargs.get("tools"))
        _trace(
            "fake model: %d messages, last %s %r, %d tools, stream=%s -> %s",
            len(messages),
            messages[-1].get("role") if messages else None,
            _text(messages[-1].get("content"))[:80] if messages else "",
            len(kwargs.get("tools") or []),
            bool(kwargs.get("stream")),
            json.dumps(answer)[:200],
        )
        if "(take your time)" in _last_user(messages)[1] and _last_tool(messages) is None:
            await asyncio.sleep(SLOW_SECONDS)
        kwargs.pop("mock_response", None)
        if "tool_calls" in answer and kwargs.get("stream"):
            # litellm's mock ignores mock_tool_calls when streaming.
            return _streamed_tool_calls(answer["tool_calls"], str(kwargs.get("model") or ""))
        if "tool_calls" in answer:
            kwargs["mock_tool_calls"] = answer["tool_calls"]
        else:
            kwargs["mock_response"] = answer["text"]
        return await real(*args, **kwargs)

    litellm.acompletion = scripted
