"""Telegram turns stream into the "Thinking..." message.

That message becomes the progress message. The runner gets an ``on_content``,
runs inside a channel-streaming scope, tool events show as a progress line, and
the answer finalises that same message instead of arriving as a second one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.channel_streaming import in_channel_stream_scope
from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.telegram import TelegramBot
from robothor.engine.telegram_format import telegram_chunks

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


@pytest.mark.asyncio
async def test_tool_events_show_on_the_progress_message(bot):
    """A running tool is a line on the Thinking... message; the answer replaces it."""
    bot._progress_edit_interval = 0.05
    run = _run()

    async def on_tool_events(**kwargs: Any) -> AgentRun:
        await kwargs["on_tool"]({"event": "tool_start", "tool": "web_search", "call_id": "c1"})
        await asyncio.sleep(0.15)
        await kwargs["on_tool"]({"event": "tool_end", "tool": "web_search", "call_id": "c1"})
        return run

    execute = AsyncMock(side_effect=on_tool_events)
    await _turn(bot, execute)

    calls = bot.bot.calls
    edits = [c[1] for c in calls if c[0] == "edit_message_text"]
    assert {e["message_id"] for e in edits} == {42}
    assert any("Searching the web" in e["text"] for e in edits[:-1]), edits
    assert edits[-1]["text"] == telegram_chunks(ANSWER)[0].html
    assert [c[0] for c in calls].count("send_message") == 1
    assert not [c for c in calls if c[0] == "delete_message"]


@pytest.mark.asyncio
async def test_a_turn_streams_into_the_thinking_message(bot):
    """AC1 end to end: one message id edited N times, then finalised in place."""
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


@pytest.mark.asyncio
async def test_a_turn_streams_even_with_the_old_flag_set_off(bot, monkeypatch):
    """No switch: ``ROBOTHOR_CHANNEL_STREAMING=0`` in the env changes nothing."""
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_CHANNEL_STREAMING", "0")
    reset_settings()
    bot._progress_edit_interval = 0.05
    run = _run()

    async def streaming(**kwargs: Any) -> AgentRun:
        text = ""
        for i in range(6):
            text += f"tide{i} "
            await kwargs["on_content"](text)
            await asyncio.sleep(0.03)
        return run

    execute = AsyncMock(side_effect=streaming)
    try:
        await _turn(bot, execute)
    finally:
        monkeypatch.delenv("ROBOTHOR_CHANNEL_STREAMING")
        reset_settings()

    assert "on_content" in execute.await_args.kwargs
    edits = [c[1] for c in bot.bot.calls if c[0] == "edit_message_text"]
    interim = [e["text"] for e in edits[:-1]]
    assert any(t.startswith("tide0") for t in interim), interim
    assert edits[-1]["message_id"] == 42
    assert edits[-1]["text"] == telegram_chunks(ANSWER)[0].html
