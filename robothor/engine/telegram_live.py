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
  addendum to someone else's request. A run whose sender is unknown takes no
  one's messages;
* a message the run never took (it arrived after the loop's last safe point,
  past the extension cap, or as the run stopped). ``_seal_live_inbox`` hands
  those back to the buffer the moment ``execute`` returns, each with its own
  sender, reply context and message id, so the race can delay a message but
  never drop it or run it as someone else.

``/stop`` discards the inbox along with the buffer, and so does shutdown.
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
    _reply_context_buffers: dict[str, dict[str, Any]]
    _pending_sender_info: dict[str, dict[str, Any]]
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
        # Busy from the moment the buffer is claimed, not from when the run task
        # exists: the "Thinking..." send in between is an await, and a message
        # landing there used to start a second, concurrent run.
        current = self._active_tasks.get(chat_id)
        if current is None or current.done():
            self._active_tasks[chat_id] = asyncio.current_task()  # type: ignore[assignment]
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
        if owner is None or owner != _sender_id(sender_info):
            return False
        message_id = self._user_message_id_buffers.get(chat_id)
        meta = {
            "attachments": list(attachments or []),
            "message_id": message_id,
            "reply_ctx": self._reply_context_buffers.get(chat_id),
            "sender_info": sender_info,
        }
        if not inbox.push(user_text, meta):
            return False
        # The handler stashed these for a drain that will not happen now; left
        # behind, they would label the next queued turn.
        self._user_message_id_buffers.pop(chat_id, None)
        self._reply_context_buffers.pop(chat_id, None)
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

    def _seal_live_inbox(self, chat_id: str, inbox: LiveInbox) -> None:
        """``execute`` returned: stop accepting, requeue what the run never took.

        Called the moment the run returns, not in ``finally``: a message sent
        while the reply is being delivered would otherwise get the 👀 of a
        message that joined, and then run as a separate turn anyway.
        """
        entry = self._live_inboxes().get(chat_id)
        if entry is not None and entry[0] is inbox:
            del self._live_inboxes()[chat_id]
        for item in inbox.close():
            self._message_buffers.setdefault(chat_id, []).append(item.text)
            if item.meta.get("attachments"):
                self._attachment_buffers.setdefault(chat_id, []).extend(item.meta["attachments"])
            for buffer, key in (
                (self._user_message_id_buffers, "message_id"),
                (self._reply_context_buffers, "reply_ctx"),
                (self._pending_sender_info, "sender_info"),
            ):
                if item.meta.get(key):
                    buffer.setdefault(chat_id, item.meta[key])

    def _finish_live_run(
        self, chat_id: str, session_key: str, session: Any, inbox: LiveInbox
    ) -> None:
        """The run's ``finally``: release the chat, requeue, drain what waits."""
        self._release_task(chat_id)
        self._seal_live_inbox(chat_id, inbox)
        self._drain_if_pending(chat_id, session_key, session)

    def _release_task(self, chat_id: str) -> None:
        """Clear the chat's busy marker only if it is still this task's.

        After ``/stop``, or an approved plan overwriting the entry, a finishing
        run popping by key hid the NEWER run from everything that asks whether
        the chat is busy.
        """
        if self._active_tasks.get(chat_id) is asyncio.current_task():
            self._active_tasks.pop(chat_id, None)

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

    def _drop_all_live_inboxes(self) -> None:
        """Shutdown, BEFORE the runs are cancelled: their ``finally`` would
        otherwise requeue into the just-cleared buffers and start a new run."""
        for chat_id in list(self._live_inboxes()):
            self._drop_live_inbox(chat_id)

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
