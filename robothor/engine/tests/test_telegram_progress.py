"""Telegram progress mode: one message, edited in place, never too often.

Telegram allows roughly one ``editMessageText`` per second per chat and answers
anything faster with a 429 carrying ``retry_after``. A model streams far faster
than that, so the progress message keeps only the LATEST text and edits on a
throttle. These tests drive it with a fake bot that records every edit with a
timestamp, and assert the properties a person in the chat would see: one
message id, edits spaced by the limit, a tool line while a tool runs and none
in the final answer, which is the existing Markdown rendering.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter

from robothor.engine.telegram_progress import TelegramProgress

INTERVAL = 0.2  # seconds; the real default is 1.0, scaled down to keep the suite fast
MESSAGE_ID = 42


class FakeBot:
    """Records edits as (monotonic time, message_id, text, parse_mode)."""

    def __init__(self, *, retry_after_on: set[int] | None = None, retry_after: int = 1) -> None:
        self.edits: list[tuple[float, int, str, Any]] = []
        self.attempts: list[float] = []
        self.sent: list[str] = []
        self._retry_after_on = retry_after_on or set()
        self._retry_after = retry_after

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, parse_mode: Any = None
    ) -> Any:
        self.attempts.append(time.monotonic())
        if len(self.attempts) in self._retry_after_on:
            raise TelegramRetryAfter(
                method=SimpleNamespace(__api_method__="editMessageText"),  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=self._retry_after,
            )
        self.edits.append((time.monotonic(), message_id, text, parse_mode))
        return SimpleNamespace(message_id=message_id, text=text)

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs.get("text", ""))
        return SimpleNamespace(message_id=MESSAGE_ID + len(self.sent))


def _progress(bot: FakeBot, **kwargs: Any) -> TelegramProgress:
    return TelegramProgress(bot, "12345", MESSAGE_ID, min_interval=INTERVAL, **kwargs)


async def _stream(progress: TelegramProgress, deltas: int, every: float) -> str:
    text = ""
    for i in range(deltas):
        text += f"word{i} "
        await progress.on_content(text)
        await asyncio.sleep(every)
    return text


@pytest.mark.asyncio
async def test_one_message_is_edited_on_a_throttle():
    """AC1: 20 deltas over ~1.2 s land as a handful of edits on ONE message id."""
    bot = FakeBot()
    progress = _progress(bot)

    text = await _stream(progress, deltas=20, every=0.06)
    await asyncio.sleep(INTERVAL * 1.5)  # the throttle catches up before the run ends
    await progress.aclose()

    ids = {e[1] for e in bot.edits}
    assert ids == {MESSAGE_ID}
    interim = bot.edits
    # ~1.2 s / 0.2 s: far fewer edits than deltas, more than one.
    assert 2 <= len(interim) <= 8, [e[2] for e in interim]
    gaps = [b[0] - a[0] for a, b in zip(bot.edits, bot.edits[1:], strict=False)]
    assert all(g >= INTERVAL * 0.95 for g in gaps), gaps
    # The last interim edit carries the latest text the model had produced.
    assert any(text.strip() in e[2] for e in interim)
    assert bot.sent == []  # the progress message never sends; deliver_reply does


@pytest.mark.asyncio
async def test_a_429_pushes_the_next_edit_out_by_retry_after():
    """AC2: after Telegram says 'retry after 1 s', nothing is attempted for 1 s."""
    bot = FakeBot(retry_after_on={1}, retry_after=1)
    progress = _progress(bot)

    await _stream(progress, deltas=15, every=0.1)
    await progress.aclose()

    assert len(bot.attempts) >= 2
    first, second = bot.attempts[0], bot.attempts[1]
    assert second - first >= 1.0 * 0.95


@pytest.mark.asyncio
async def test_tool_line_shows_while_a_tool_runs_and_goes_when_it_ends():
    """AC3: the tool's label and elapsed seconds, gone once the tool ends."""
    bot = FakeBot()
    progress = _progress(bot, tick_interval=INTERVAL)

    await progress.on_content("Let me look that up.")
    await progress.on_event({"event": "tool_start", "tool": "web_search", "call_id": "c1"})
    await asyncio.sleep(1.3)
    tool_edits = [e for e in bot.edits if "search" in e[2].lower()]
    assert tool_edits, [e[2] for e in bot.edits]
    assert any("1s" in e[2] for e in tool_edits), [e[2] for e in tool_edits]
    await progress.on_event({"event": "tool_end", "tool": "web_search", "call_id": "c1"})

    await asyncio.sleep(INTERVAL * 2)
    await progress.aclose()

    assert "search" not in bot.edits[-1][2].lower()
    assert bot.edits[-1][2] == "Let me look that up."


@pytest.mark.asyncio
async def test_stream_retry_discards_the_partial_text():
    """A retried stream starts over: the partial text is not left on screen."""
    bot = FakeBot()
    progress = _progress(bot)

    await progress.on_content("partial answer that")
    await asyncio.sleep(INTERVAL * 1.5)
    await progress.on_event({"event": "stream_retry", "model": "m/a"})
    await asyncio.sleep(INTERVAL * 1.5)
    await progress.on_content("Fresh")
    await asyncio.sleep(INTERVAL * 1.5)
    await progress.aclose()

    assert "partial answer" in bot.edits[0][2]
    assert "partial answer" not in bot.edits[-1][2]
    assert "Fresh" in bot.edits[-1][2]


@pytest.mark.asyncio
async def test_progress_note_is_shown_while_nothing_else_is():
    """The periodic 'still working' note is not lost in progress mode."""
    bot = FakeBot()
    progress = _progress(bot)

    await progress.on_event({"event": "progress", "text": "Still working (2m)"})
    await asyncio.sleep(INTERVAL * 1.5)
    await progress.aclose()

    assert any("Still working" in e[2] for e in bot.edits)


@pytest.mark.asyncio
async def test_interim_text_is_truncated_to_what_telegram_accepts():
    bot = FakeBot()
    progress = _progress(bot)

    await progress.on_content("y" * 9000)
    await asyncio.sleep(INTERVAL * 1.5)
    await progress.aclose()

    assert bot.edits
    assert all(len(e[2]) <= 4096 for e in bot.edits)


@pytest.mark.asyncio
async def test_no_edit_after_close():
    bot = FakeBot()
    progress = _progress(bot)

    await progress.on_content("one")
    await progress.aclose()
    count = len(bot.edits)
    await progress.on_content("one two")
    await asyncio.sleep(INTERVAL * 2)

    assert len(bot.edits) == count


def _gaps(bot: FakeBot) -> list[float]:
    return [b[0] - a[0] for a, b in zip(bot.edits, bot.edits[1:], strict=False)]


@pytest.mark.asyncio
async def test_a_group_chat_edits_at_the_group_safe_interval(monkeypatch):
    """Groups have stricter flood limits than a private chat: their edits are spaced wider."""
    from robothor.engine import telegram_progress

    monkeypatch.setattr(telegram_progress, "GROUP_MIN_INTERVAL", INTERVAL * 3, raising=False)
    bot = FakeBot()
    progress = TelegramProgress(bot, "-100123", MESSAGE_ID, min_interval=INTERVAL)

    await _stream(progress, deltas=24, every=0.06)
    await progress.aclose()

    assert len(bot.edits) >= 2
    assert all(g >= INTERVAL * 3 * 0.95 for g in _gaps(bot)), _gaps(bot)


@pytest.mark.asyncio
async def test_a_429_widens_the_interval_from_then_on():
    """Adaptive: after flood control answers once, later edits are spaced at least twice as far."""
    bot = FakeBot(retry_after_on={1}, retry_after=0)
    progress = _progress(bot)

    await _stream(progress, deltas=30, every=0.06)
    await progress.aclose()

    assert len(bot.edits) >= 2
    assert all(g >= INTERVAL * 2 * 0.95 for g in _gaps(bot)), _gaps(bot)


@pytest.mark.asyncio
async def test_a_long_retry_after_never_holds_the_answer_back():
    """The total wait is bounded: delivery stops the progress worker, it does not wait it out."""
    from robothor.engine.telegram_progress import deliver_reply

    bot = FakeBot(retry_after_on={1}, retry_after=30)
    progress = _progress(bot)
    await progress.on_content("partial")
    await asyncio.sleep(0.05)  # the first edit hits the 429; the worker now owes 30 s

    async def send(_chat_id: str, text: str) -> list[Any]:
        return [await bot.send_message(text=text)]

    run = SimpleNamespace(output_text="The answer.", error_message=None)
    started = time.monotonic()
    sent = await asyncio.wait_for(deliver_reply(progress, run, "12345", send), 2)

    assert time.monotonic() - started < 1.0
    assert bot.sent == ["The answer."]
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "label"),
    [
        pytest.param(
            {"name": "web_search", "arguments": {"q": "tides"}}, "Searching", id="wrapped"
        ),
        pytest.param({"name": "  web_search "}, "Searching", id="padded-name"),
        pytest.param({}, "Tool Call", id="no-name"),
        pytest.param({"name": "tool_call"}, "Tool Call", id="nested-wrapper"),
        pytest.param({"name": 7}, "Tool Call", id="not-a-string"),
    ],
)
async def test_a_tool_call_wrapper_is_labelled_by_the_tool_it_wraps(args, label):
    """``tool_call(name=X)`` shows X's label; a malformed wrapper stays "Tool Call"."""
    from robothor.engine.telegram import _friendly_tool_name

    bot = FakeBot()
    progress = _progress(bot, label_for=_friendly_tool_name)

    await progress.on_event({"event": "tool_start", "tool": "tool_call", "args": args})
    await asyncio.sleep(INTERVAL / 2)
    await progress.aclose()

    assert bot.edits, "the tool line was never shown"
    line = bot.edits[-1][2]
    assert label in line, line
    if label != "Tool Call":
        assert "Tool Call" not in line, line
