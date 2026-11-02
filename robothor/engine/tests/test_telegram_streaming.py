"""Telegram turns stream into the "Thinking..." message.

That message becomes the progress message. The runner gets an ``on_content``,
runs inside a channel-streaming scope, and tool events show as a progress line.
The answer then goes out as a NEW message, because Telegram does not notify on
an edit, and the progress message is deleted once the answer has landed, so the
chat ends with one message, the answer, and the person is notified of it.
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


def _assert_answer_sent_then_progress_deleted(calls: list[tuple[str, dict[str, Any]]]) -> None:
    """The answer is a fresh send (it notifies); the progress message goes after it."""
    names = [c[0] for c in calls]
    sends = [i for i, n in enumerate(names) if n == "send_message"]
    assert len(sends) == 2, "the Thinking... message, then the answer"
    answer = calls[sends[1]][1]
    assert answer["text"] == telegram_chunks(ANSWER)[0].html
    assert answer["parse_mode"] == "HTML"
    deletes = [i for i, n in enumerate(names) if n == "delete_message"]
    assert [calls[i][1]["message_id"] for i in deletes] == [42]
    assert deletes[0] > sends[1], "the progress message stays until the answer has landed"
    edits = [c[1] for c in calls if c[0] == "edit_message_text"]
    assert all(e["message_id"] == 42 for e in edits)
    assert not [e for e in edits if e["text"] == answer["text"]], "the answer is not an edit"


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
    """A running tool is a line on the Thinking... message; the answer follows it."""
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
    assert any("Searching the web" in e["text"] for e in edits), edits
    _assert_answer_sent_then_progress_deleted(calls)


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
    edits = [c for c in calls if c[0] == "edit_message_text"]
    assert len(edits) >= 2  # interim edits while the answer streams
    _assert_answer_sent_then_progress_deleted(calls)
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
    assert any(e["text"].startswith("tide0") for e in edits), edits
    _assert_answer_sent_then_progress_deleted(bot.bot.calls)


@pytest.mark.asyncio
async def test_a_failed_answer_send_keeps_the_streamed_text(bot):
    """The answer never landed: the progress message, holding the streamed text, stays."""
    bot._progress_edit_interval = 0.05
    run = _run()

    async def streaming(**kwargs: Any) -> AgentRun:
        await kwargs["on_content"]("tide0 ")
        await asyncio.sleep(0.1)
        return run

    bot.send_message = AsyncMock(return_value=[])
    await _turn(bot, AsyncMock(side_effect=streaming))

    assert not [c for c in bot.bot.calls if c[0] == "delete_message"]
    assert run.delivery_status is not None and run.delivery_status.startswith("failed")
