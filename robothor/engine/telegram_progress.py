"""Telegram progress message: one message, edited in place, never too often.

The "Thinking..." message a Telegram turn starts with becomes the place the
answer is written as it streams: the model's cumulative text, plain, plus a
compact line for the tool that is running ("Searching the web · 4s"). When the
run ends, :func:`deliver_reply` sends the formatted answer as a new message
(Telegram notifies on a send, never on an edit) and then deletes the progress
message, so the chat is left with the answer alone.

Telegram allows about one ``editMessageText`` per second in a private chat, less
in a group, and answers anything faster with a 429 carrying ``retry_after``. A
model streams far faster than that, so this keeps only the LATEST state and a
single background worker edits on a throttle: never sooner than
``min_interval`` after the previous edit (``GROUP_MIN_INTERVAL`` in a group),
never sooner than a 429 said, and each 429 doubles the spacing for the rest of
the turn, up to ``MAX_INTERVAL``. Neither the stream nor the answer ever waits
on it: :func:`deliver_reply` stops the worker and sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramRetryAfter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = ["TelegramProgress", "deliver_reply"]

#: Telegram's message length limit, and the room left for the tool line.
_MESSAGE_LIMIT = 4096
_TEXT_BUDGET = 3900

#: Seconds between edits in a group or supergroup (negative chat id). Telegram
#: allows a group about 20 messages a minute, and edits are reported to count.
GROUP_MIN_INTERVAL = 3.0

#: Ceiling for the interval a 429 doubles, so a burst of flood control cannot
#: freeze the progress message for minutes.
MAX_INTERVAL = 12.0

#: How often an edit may refresh only the elapsed seconds of a running tool.
_DEFAULT_TICK = 3.0


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
        if str(chat_id).startswith("-"):
            min_interval = max(min_interval, GROUP_MIN_INTERVAL)
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
                # Back off adaptively: honour retry_after now, and space every
                # later edit of this turn twice as far apart.
                self._next_allowed = time.monotonic() + float(e.retry_after)
                self._min_interval = min(self._min_interval * 2, MAX_INTERVAL)
                self._tick = max(self._tick, self._min_interval)
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


async def deliver_reply(
    progress: TelegramProgress | None,
    run: Any,
    chat_id: str,
    send: Callable[[str, str], Awaitable[list[Any]]],
) -> list[Any]:
    """Deliver a finished run's answer; the one place a Telegram turn's reply goes out.

    The answer (or the error, or "no output") always goes out through
    ``send(chat_id, text)`` as new messages: Telegram does not notify on an
    edit, and someone who put the phone down must still hear the answer
    arrive. The progress message stops and is deleted once the answer has
    landed, so the chat ends with the answer alone; if nothing landed, it
    stays, holding the streamed text. ``progress`` is ``None`` only when
    Telegram refused the "Thinking..." message. Returns the delivered Message
    objects, so the caller can record delivery and map platform message ids.
    """
    if progress is not None:
        await progress.aclose()
    if run.output_text:
        sent = await send(chat_id, run.output_text)
    elif run.error_message:
        sent = await send(chat_id, f"Error: {run.error_message}")
    else:
        sent = await send(chat_id, "Done. No output produced.")
    if progress is not None and sent:
        with contextlib.suppress(Exception):
            await progress.bot.delete_message(
                chat_id=int(progress.chat_id), message_id=progress.message_id
            )
    return sent
