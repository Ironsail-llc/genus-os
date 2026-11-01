"""Telegram progress mode: one message, edited in place, never too often.

With ``ROBOTHOR_CHANNEL_STREAMING`` on, the "Thinking..." message a Telegram
turn starts with becomes the place the answer is written: the model's
cumulative text, plain, plus a compact line for the tool that is running
("Searching the web · 4s"). When the run ends, :meth:`TelegramProgress.finalise`
turns the same message into the formatted answer through the existing Markdown
renderer (``telegram_format.telegram_chunks``), so a reply looks exactly as it
did before, only sooner.

Telegram allows about one ``editMessageText`` per second per chat and answers
anything faster with a 429 carrying ``retry_after``. A model streams far faster
than that, so this keeps only the LATEST state and a single background worker
edits on a throttle: never sooner than ``min_interval`` after the previous
edit, never sooner than a 429 said. The stream itself never waits on Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from robothor.engine.telegram_format import SentMessages, needs_rich_message, telegram_chunks

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = ["TelegramProgress"]

#: Telegram's message length limit, and the room left for the tool line.
_MESSAGE_LIMIT = 4096
_TEXT_BUDGET = 3900

#: How often an edit may refresh only the elapsed seconds of a running tool.
_DEFAULT_TICK = 3.0

#: Final-edit attempts on a 429 before handing the answer to the chunked sender.
_FINAL_RETRIES = 3


def _default_label(tool: str) -> str:
    return tool.replace("_", " ").strip() or "tool"


class TelegramProgress:
    """The live progress message for one Telegram turn."""

    def __init__(
        self,
        bot: Any,
        chat_id: str,
        message_id: int,
        *,
        min_interval: float = 1.0,
        tick_interval: float = _DEFAULT_TICK,
        label_for: Callable[[str], str] = _default_label,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self._min_interval = min_interval
        self._tick = max(tick_interval, min_interval)
        self._label_for = label_for
        self._text = ""
        self._note = ""
        self._tool: str | None = None
        self._tool_started = 0.0
        self._next_allowed = 0.0
        self._last_rendered: str | None = None
        self._last_state: tuple[str, str | None, str] | None = None
        self._last_edit = 0.0
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    # ── inputs ────────────────────────────────────────────────────────

    async def on_content(self, cumulative: str) -> None:
        """The cumulative text of the current model call; replaces what is shown."""
        self._text = cumulative
        self._kick()

    async def on_event(self, event: dict[str, Any]) -> None:
        """A runner tool/status event: tool start and end, a retry, a progress note."""
        kind = event.get("event")
        if kind == "tool_start":
            self._tool = self._label_for(str(event.get("tool", "")))
            self._tool_started = time.monotonic()
        elif kind == "tool_end":
            self._tool = None
        elif kind == "stream_retry":
            self._text = ""  # the partial answer is being thrown away; so is its display
        elif kind == "progress":
            self._note = str(event.get("text", ""))
        else:
            return
        self._kick()

    # ── output ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Stop editing. Idempotent; no edit happens after it returns."""
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker

    async def finalise(self, text: str) -> SentMessages | None:
        """Turn the progress message into the formatted answer.

        Returns the delivered messages (a list holding this one message), or
        ``None`` when the answer does not fit one plain edit — several chunks,
        or a table Telegram draws as a rich message — or Telegram refused the
        edit. ``None`` means the caller sends it the usual way.
        """
        await self.aclose()
        if not text or needs_rich_message(text):
            return None
        chunks = telegram_chunks(text)
        if len(chunks) != 1:
            return None
        chunk = chunks[0]
        for body, mode in ((chunk.html, ParseMode.HTML), (chunk.plain, None)):
            message = await self._final_edit(body, mode)
            if message is not None:
                return SentMessages([message], expected=1)
        return None

    # ── internals ─────────────────────────────────────────────────────

    def _kick(self) -> None:
        if self._closed:
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name=f"tg-progress:{self.chat_id}")

    def _body(self) -> str:
        text = self._text.strip()
        if len(text) > _TEXT_BUDGET:
            text = "…" + text[-(_TEXT_BUDGET - 1) :]
        return text

    def _render(self) -> str:
        parts = [self._body()]
        if self._tool:
            elapsed = int(time.monotonic() - self._tool_started)
            parts.append(f"\U0001f527 {self._tool} · {elapsed}s")
        elif self._note and not parts[0]:
            parts.append(self._note)
        rendered = "\n\n".join(p for p in parts if p)
        return rendered[:_MESSAGE_LIMIT]

    async def _run(self) -> None:
        while not self._closed:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            rendered = self._render()
            if not rendered or rendered == self._last_rendered:
                if self._tool is None:
                    return
                await asyncio.sleep(self._min_interval)  # a tool is running: tick
                continue
            state = (self._body(), self._tool, self._note)
            if state == self._last_state and self._tool is not None:
                # Only the elapsed seconds moved: refresh at the slower tick.
                wait = self._last_edit + self._tick - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
            try:
                await self._edit(rendered, None)
            except TelegramRetryAfter as e:
                self._next_allowed = time.monotonic() + float(e.retry_after)
                continue
            except Exception as e:  # noqa: BLE001 - a lost interim edit is not a lost answer
                logger.debug("Telegram progress edit failed: %s", type(e).__name__)
            self._last_rendered = rendered
            self._last_state = state
            self._last_edit = time.monotonic()
            self._next_allowed = self._last_edit + self._min_interval

    async def _edit(self, text: str, mode: Any) -> Any:
        return await self.bot.edit_message_text(
            chat_id=int(self.chat_id),
            message_id=self.message_id,
            text=text,
            parse_mode=mode,
        )

    async def _final_edit(self, text: str, mode: Any) -> Any:
        for _ in range(_FINAL_RETRIES):
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await self._edit(text, mode)
            except TelegramRetryAfter as e:
                self._next_allowed = time.monotonic() + float(e.retry_after)
                continue
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    return SimpleNamespace(message_id=self.message_id)
                logger.debug("Telegram refused the final edit: %s", e)
                return None
            except Exception as e:  # noqa: BLE001 - fall back to a fresh send
                logger.debug("Telegram final edit failed: %s", type(e).__name__)
                return None
            if getattr(result, "message_id", None) is None:
                return SimpleNamespace(message_id=self.message_id)
            return result
        return None
