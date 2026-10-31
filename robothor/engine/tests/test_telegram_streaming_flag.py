"""Telegram turns with ROBOTHOR_CHANNEL_STREAMING off and on.

Off: the turn is today's turn, call for call — ``runner.execute`` gets the same
keywords (no ``on_content``, so the LLM call stays non-streaming) and the bot
sees the same sequence of sends, edits and deletes. That is pinned against a
committed fixture recorded from the code before this change.

On: the "Thinking..." message becomes the progress message. The runner gets an
``on_content``, runs inside a channel-streaming scope, and the answer finalises
that same message instead of arriving as a second one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.channel_streaming import in_channel_stream_scope
from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.telegram import TelegramBot
from robothor.engine.telegram_format import telegram_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_turn_flag_off.json"
ANSWER = "**Tides** rise twice a day."


class RecordingBot:
    """The aiogram ``Bot`` surface a turn touches, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 42

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, {k: _plain(v) for k, v in kwargs.items()}))

    async def send_message(self, **kwargs: Any) -> Any:
        self._record("send_message", kwargs)
        msg = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return msg

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self._record("edit_message_text", kwargs)
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs: Any) -> Any:
        self._record("delete_message", kwargs)
        return True

    async def send_chat_action(self, **kwargs: Any) -> Any:  # timing-dependent; not recorded
        return True


def _plain(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return str(value.value)
    if callable(value):
        return "<callable>"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return f"<{type(value).__name__}>"


@pytest.fixture
def bot(engine_config):
    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot"),
        patch("robothor.engine.telegram.Dispatcher"),
        patch("robothor.engine.delivery._persist_delivery_status", new_callable=AsyncMock),
        patch("robothor.engine.telegram.save_exchange_async", new=AsyncMock(return_value=None)),
    ):
        tg = TelegramBot(engine_config, MagicMock())
        tg.bot = RecordingBot()
        yield tg
    _sessions.clear()


def _run() -> AgentRun:
    return AgentRun(
        id="run-tg-stream",
        status=RunStatus.COMPLETED,
        output_text=ANSWER,
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="chat:12345",
    )


async def _turn(tg: TelegramBot, execute: Any) -> None:
    tg.runner.execute = execute
    session_key = tg._session_key("12345")
    session = get_shared_session(session_key)
    await tg._run_interactive("12345", session_key, session, "hello")
    task = tg._active_tasks.get("12345")
    if task:
        await task


def _execute_kwargs(execute: AsyncMock) -> dict[str, Any]:
    return {k: _plain(v) for k, v in execute.await_args.kwargs.items()}


@pytest.mark.asyncio
async def test_flag_off_turn_matches_the_fixture(bot, channel_streaming):
    """AC8: kwargs and bot calls identical to the recording made before the change."""
    channel_streaming(False)
    run = _run()

    async def on_tool_events(**kwargs: Any) -> AgentRun:
        await kwargs["on_tool"]({"event": "tool_start", "tool": "web_search", "call_id": "c1"})
        await kwargs["on_tool"]({"event": "tool_end", "tool": "web_search", "call_id": "c1"})
        return run

    execute = AsyncMock(side_effect=on_tool_events)
    await _turn(bot, execute)

    # Through JSON so tuples compare as the lists the fixture holds.
    observed = json.loads(json.dumps({"execute": _execute_kwargs(execute), "bot": bot.bot.calls}))
    if os.environ.get("REGENERATE_TELEGRAM_FIXTURE"):
        FIXTURE.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
    assert observed == json.loads(FIXTURE.read_text())
    assert "on_content" not in execute.await_args.kwargs


@pytest.mark.asyncio
async def test_flag_on_streams_into_the_thinking_message(bot, channel_streaming):
    """AC1 end to end: one message id edited N times, then finalised in place."""
    channel_streaming(True)
    bot._progress_edit_interval = 0.1
    run = _run()
    scoped: list[bool] = []

    async def streaming(**kwargs: Any) -> AgentRun:
        scoped.append(in_channel_stream_scope())
        text = ""
        for i in range(12):
            text += f"tide{i} "
            await kwargs["on_content"](text)
            await asyncio.sleep(0.04)
        return run

    execute = AsyncMock(side_effect=streaming)
    await _turn(bot, execute)

    assert "on_content" in execute.await_args.kwargs
    assert scoped == [True]
    calls = bot.bot.calls
    sends = [c for c in calls if c[0] == "send_message"]
    edits = [c for c in calls if c[0] == "edit_message_text"]
    assert len(sends) == 1, "only the Thinking... message is sent"
    thinking_id = 42
    assert {e[1]["message_id"] for e in edits} == {thinking_id}
    assert len(edits) >= 3  # at least two interim edits, then the final one
    assert edits[-1][1]["text"] == telegram_chunks(ANSWER)[0].html
    assert not [c for c in calls if c[0] == "delete_message"]
    assert run.delivery_status == "delivered"
