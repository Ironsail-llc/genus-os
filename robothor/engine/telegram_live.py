"""Telegram messages that arrive while the chat's run is still working.

Each interactive run gets a ``LiveInbox`` (robothor/engine/live_inbox.py). A
message sent during the run goes into it and the run picks it up at its next
safe point, the way Claude Code and Codex take input mid-task. Before this, the
message waited in the coalescing buffer and went out as its own turn once the
run finished: two answers, the first written without the new context.

What does not join a live run, and still waits for its own turn:

* anything the handler intercepts first (an ``ask_user`` answer, plan-mode
  replies). Those never reach ``_enqueue_message``;
* a message from a different sender in a group chat. It is theirs, not an
  addendum to someone else's request;
* a message the run never took (it arrived after the loop's last safe point,
  or past the extension cap). ``_finish_live_run`` hands those back to the
  buffer, so the race can delay a message but never drop it.

``/stop`` discards the inbox along with the buffer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram.types import ReactionTypeEmoji

from robothor.engine.live_inbox import LiveInbox, history_text

logger = logging.getLogger(__name__)

#: The acknowledgement on a message that joined the live run. It tells the
#: operator "the agent has this" apart from "this is waiting its turn".
JOINED_REACTION = "\U0001f440"


def _sender_id(sender_info: dict[str, Any] | None) -> str | None:
    value = (sender_info or {}).get("telegram_user_id")
    return str(value) if value else None


class TelegramLiveFollowupMixin:
    """Mixed into ``TelegramBot``; uses its buffers, tasks and sender."""

    _active_tasks: dict[str, asyncio.Task[Any]]
    _message_buffers: dict[str, list[str]]
    _drain_scheduled: dict[str, bool]
    _attachment_buffers: dict[str, list[dict[str, Any]]]
    _user_message_id_buffers: dict[str, str]
    bot: Any

    def _live_inboxes(self) -> dict[str, tuple[LiveInbox, str | None]]:
        inboxes: dict[str, tuple[LiveInbox, str | None]] = self.__dict__.setdefault(
            "_live_inbox_by_chat", {}
        )
        return inboxes

    def _open_live_inbox(self, chat_id: str, sender_info: dict[str, Any] | None) -> LiveInbox:
        async def on_interim(text: str) -> None:
            await self.send_message(chat_id, text)  # type: ignore[attr-defined]

        inbox = LiveInbox(on_interim=on_interim)
        self._live_inboxes()[chat_id] = (inbox, _sender_id(sender_info))
        return inbox

    async def _join_live_run(
        self,
        chat_id: str,
        user_text: str,
        sender_info: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None,
    ) -> bool:
        """True when the message went to the running agent instead of the buffer."""
        task = self._active_tasks.get(chat_id)
        entry = self._live_inboxes().get(chat_id)
        if task is None or task.done() or entry is None:
            return False
        inbox, owner = entry
        sender = _sender_id(sender_info)
        if owner and sender and owner != sender:
            return False
        message_id = self._user_message_id_buffers.get(chat_id)
        meta = {"attachments": list(attachments or []), "message_id": message_id}
        if not inbox.push(user_text, meta):
            return False
        # The handler stashed this message's id for a drain that will not
        # happen now; leaving it would mislabel the next queued turn.
        self._user_message_id_buffers.pop(chat_id, None)
        await self._ack_joined(chat_id, message_id)
        return True

    async def _ack_joined(self, chat_id: str, message_id: str | None) -> None:
        if not message_id:
            return
        with contextlib.suppress(Exception):
            await self.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=int(message_id),
                reaction=[ReactionTypeEmoji(emoji=JOINED_REACTION)],
            )

    def _finish_live_run(
        self, chat_id: str, session_key: str, session: Any, inbox: LiveInbox
    ) -> None:
        """Close the run's inbox, requeue what it never took, drain the buffer."""
        entry = self._live_inboxes().get(chat_id)
        if entry is not None and entry[0] is inbox:
            del self._live_inboxes()[chat_id]
        for item in inbox.close():
            self._message_buffers.setdefault(chat_id, []).append(item.text)
            if item.meta.get("attachments"):
                self._attachment_buffers.setdefault(chat_id, []).extend(item.meta["attachments"])
            if item.meta.get("message_id"):
                self._user_message_id_buffers.setdefault(chat_id, item.meta["message_id"])
        self._drain_if_pending(chat_id, session_key, session)

    def _drain_if_pending(self, chat_id: str, session_key: str, session: Any) -> None:
        """Start the next turn for anything that queued behind a finished run."""
        if self._message_buffers.get(chat_id) and not self._drain_scheduled.get(chat_id):
            self._drain_scheduled[chat_id] = True
            asyncio.create_task(self._drain_and_run(chat_id, session_key, session))  # type: ignore[attr-defined]

    def _drop_live_inbox(self, chat_id: str) -> None:
        """``/stop``: what was sent mid-run goes with the run."""
        entry = self._live_inboxes().pop(chat_id, None)
        if entry is not None:
            entry[0].close()

    def _append_user_turn(self, session: Any, inbox: LiveInbox, user_text: str) -> None:
        """The request as the conversation keeps it: plus what joined mid-run."""
        session.history.append({"role": "user", "content": self._live_turn_text(inbox, user_text)})

    @staticmethod
    def _live_turn_text(inbox: LiveInbox, user_text: str) -> str:
        return history_text(user_text, inbox.taken)

    @staticmethod
    def _live_attachments(
        inbox: LiveInbox, attachments: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        added = [row for item in inbox.taken for row in item.meta.get("attachments") or []]
        return [*(attachments or []), *added] or None
