"""Explicit private-input boundary, before Telegram logging, enrichment or history.

Only typed `/secure` messages are captured. Ordinary prose is not reliably
classifiable as a secret. The authenticated dashboard remains the card input.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING

from pydantic import SecretStr

from robothor.autonomy.enrollment import EnrollmentRequest, EnrollmentStore
from robothor.autonomy.identity import scope_for_actor
from robothor.autonomy.models import ResourceInput
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from typing import TypeAlias

    from aiogram.types import Message

    from robothor.engine.telegram_attachments import TelegramAttachmentsMixin
    from robothor.engine.telegram_handlers import TelegramHandlersMixin

    IntakeHost: TypeAlias = TelegramAttachmentsMixin | TelegramHandlersMixin

logger = logging.getLogger(__name__)

_COMMAND = re.compile(r"^/secure(?:@[a-zA-Z0-9_]+)?(?:\s|$)", re.IGNORECASE)
_HELP = (
    "Open Account → Personal automation to save private information. "
    "For a secure input link, send /secure profile, /secure document, "
    "or /secure credential https://example.com. Card details belong on that page."
)


def is_secure(text: str) -> bool:
    return bool(_COMMAND.match(text.strip()))


def _parse(text: str) -> tuple[EnrollmentRequest, str]:
    rest = _COMMAND.sub("", text.strip(), count=1).strip()
    parts = rest.split(maxsplit=1)
    kind = parts[0] if parts else "profile"
    rest = parts[1] if len(parts) > 1 else ""
    origin = None
    if kind in {"credential", "totp"}:
        parts = rest.split(maxsplit=1)
        origin = parts[0] if parts else None
        rest = parts[1] if len(parts) > 1 else ""
    return EnrollmentRequest(kind=kind, origin=origin), rest  # type: ignore[arg-type]


async def _unauthorized(bot: IntakeHost, message: Message) -> bool:
    """Answer a sender this instance does not know, without describing itself.

    ``_HELP`` names the dashboard path and the command grammar. That is the
    right answer for someone who is already inside, and the wrong one for a
    stranger: it fingerprints the instance. Worse, it was the ONLY answer —
    ``intercept`` ran as the first statement of ``handle_text``, before
    identity resolution, so the ordinary path's
    ``_notify_operator_of_unregistered_sender`` never fired and the operator
    never heard that a stranger had reached the bot.
    """
    if not message.from_user:
        return True
    reply = await bot._handle_unregistered_sender(message, str(message.from_user.id))
    if reply:
        await message.answer(reply, parse_mode=None)
    return True


async def intercept(bot: IntakeHost, message: Message, *, attachment: bool = False) -> bool:
    text = (message.caption if attachment else message.text) or ""
    if not is_secure(text):
        return False
    # Authorization comes BEFORE this boundary says anything about itself, and
    # a marked input is consumed either way: the raw text must never reach the
    # log, onboarding, a pending ask or the model, whoever sent it.
    if not message.from_user:
        return True
    user = bot._resolve_user(str(message.chat.id), message)
    if not user or not user.get("user_id") or not user.get("tenant_id"):
        return await _unauthorized(bot, message)
    if message.chat.type != "private":
        # A known sender in a shared room. Refuse without reciting the
        # dashboard path to everyone else in it.
        await message.answer(
            "Private input isn't accepted in a group chat. Send it to me in a direct message.",
            parse_mode=None,
        )
        return True
    try:
        scope = await asyncio.to_thread(scope_for_actor, user["tenant_id"], user["user_id"])
        spec, raw = _parse(text)
        store = AutonomyStore()
        if attachment:
            if spec.kind != "document" or raw or message.media_group_id:
                raise ValueError
            from robothor.engine.telegram_attachments import media_ref

            media = media_ref(message)
            if not media or (media.size and media.size > 5_000_000):
                raise ValueError
            content = await bot._download_media(media)
            if not content or len(content) > 5_000_000:
                raise ValueError
            raw = json.dumps(
                {
                    "name": media.name,
                    "mime_type": media.mime or "application/octet-stream",
                    "base64": base64.b64encode(content).decode(),
                }
            )
        if not raw:
            link = await asyncio.to_thread(EnrollmentStore(store).create, scope, spec)
            await message.answer(
                "Save your information using this link (expires in 15 minutes):\n"
                + (link["url"] or link["path"]),
                parse_mode=None,
            )
            return True
        if spec.kind == "payment_card":
            await message.answer(_HELP, parse_mode=None)
            return True
        labels = {
            "profile": "Personal profile",
            "credential": "Website login",
            "totp": "Website authenticator",
            "document": "Private document",
        }
        resource = ResourceInput(
            kind=spec.kind, label=labels[spec.kind], origin=spec.origin, payload=SecretStr(raw)
        )
        receipt = await asyncio.to_thread(store.put_resource, scope, resource)
    except Exception:
        await message.answer("Private input was not saved. " + _HELP, parse_mode=None)
        return True
    from robothor.engine.chat import get_shared_session

    chat_id = str(message.chat.id)
    session_key = bot._session_key(chat_id)
    note = f"Private {spec.kind} saved as resource {receipt['id']}. Use this reference for my authorized task."
    await bot._enqueue_message(
        chat_id, session_key, get_shared_session(session_key), note, sender_info=user
    )
    return True


def collect_album(bot: IntakeHost, message: Message) -> None:
    """Hold album metadata until its caption arrives, before downloading any member.

    Secure enrollment supports individual files. A batch with a secure caption is
    refused as a whole, including captionless members arriving in this window.
    Tasks use the bot's existing shutdown registry.

    KNOWN COST, not yet paid down: this buffer sits IN FRONT of the ordinary
    album buffer in ``telegram_attachments``, so an album now waits one window
    here before the first byte is downloaded, then the re-dispatched members
    start a second window there. Two consequences, both real:

    * an album is delivered roughly ``2 * ALBUM_WINDOW_SECONDS`` later than it
      was before this boundary existed;
    * the members are downloaded one at a time, and if any single download
      takes longer than a window the ordinary buffer's timer fires mid-loop
      and one album arrives as two turns.

    Collapsing the two buffers into one is the fix and it is not a small
    change: the ordinary flush's late-member handling (re-reviews R4/I4) is
    load-bearing and would have to be rewritten around a caption check. Doing
    it badly costs the operator a silently dropped photo, which is the exact
    failure this whole area already has a history of. Left deliberately.
    """
    key = (str(message.chat.id), "intake:" + str(message.media_group_id))
    pending = bot._album_buffers.get(key)
    if pending is None:
        pending = {"messages": [], "private": False, "dropped": 0}
        bot._album_buffers[key] = pending
        bot._album_tasks.setdefault(key, set()).add(asyncio.create_task(_flush_intake(bot, key)))
    # EVERY member's caption decides `private`, including one past the cap.
    # That is what makes the graceful path below safe: the marked-batch
    # property does not depend on the member's `Message` being buffered.
    pending["private"] = pending["private"] or is_secure(message.caption or "")
    if len(pending["messages"]) < 25:
        pending["messages"].append(message)
    else:
        # Only the `Message` object is dropped, to bound what one sender-chosen
        # media_group_id can make this process hold. Forcing `private` here
        # answered an ordinary 26-photo batch with "Private albums were not
        # saved", which is false — nothing about it was private — and kept
        # none of the 25 that did fit. The ordinary album path has always
        # degraded with a count instead.
        pending["dropped"] = int(pending.get("dropped", 0)) + 1


async def _flush_intake(bot: IntakeHost, key: tuple[str, str]) -> None:
    from robothor.engine.telegram_attachments import ALBUM_WINDOW_SECONDS

    mine = asyncio.current_task()
    try:
        await asyncio.sleep(ALBUM_WINDOW_SECONDS)
        pending = bot._album_buffers.pop(key, None)
        if not pending:
            return
        if pending["private"]:
            await pending["messages"][0].answer(
                "Private albums were not saved. Send each file separately with /secure document, "
                "or use Account → Personal automation.",
                parse_mode=None,
            )
            return
        # ONE try/except around the whole loop lost the album. Before this
        # detour existed, each member was its own aiogram update task: a member
        # that blew up cost exactly that member, and `handle_file` still
        # answered "I couldn't download ...". Collecting them into a single
        # loop under this boundary's deliberately silent `except` meant the
        # first member to raise ended the loop and said nothing — a four-photo
        # album where member 1 failed delivered member 0 and no reply at all.
        #
        # This branch is the ORDINARY path (`private` is False), so each member
        # owes its own sender an answer, exactly as it did before. The answer
        # names no file and quotes no exception, because a caption on a member
        # that arrives after this window is not visible from here.
        dropped = int(pending.get("dropped", 0))
        if dropped:
            await pending["messages"][0].answer(
                f"That batch had {dropped} more file(s) than I can take at once. I'm keeping "
                f"the first {len(pending['messages'])} — send the rest in another batch.",
                parse_mode=None,
            )
        for member in pending["messages"]:
            try:
                await bot.handle_file(member, _album_checked=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the operator
                logger.warning("An album member could not be kept: %s", type(exc).__name__)
                with contextlib.suppress(Exception):
                    await member.answer(
                        "I couldn't keep one of the files in that batch, so it isn't in my "
                        "inbox. Send that one again on its own and I'll try once more.",
                        parse_mode=None,
                    )
    except Exception:
        # No raw message/filename/exception leaves this boundary on failure.
        return
    finally:
        tasks = bot._album_tasks.get(key)
        if tasks is not None:
            tasks.discard(mine)
            if not tasks:
                bot._album_tasks.pop(key, None)
