"""Explicit private-input boundary, before Telegram logging, enrichment or history.

Only typed `/secure` messages are captured. Ordinary prose is not reliably
classifiable as a secret. The authenticated dashboard remains the card input.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import TYPE_CHECKING

from pydantic import SecretStr

from robothor.autonomy.enrollment import EnrollmentRequest, EnrollmentStore
from robothor.autonomy.identity import scope_for_actor
from robothor.autonomy.models import ResourceInput
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from aiogram.types import Message

    from robothor.engine.telegram import TelegramBot

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


async def intercept(bot: TelegramBot, message: Message, *, attachment: bool = False) -> bool:
    text = (message.caption if attachment else message.text) or ""
    if not is_secure(text):
        return False
    # A marked input is ALWAYS consumed here, including failures. Never log raw
    # exceptions, invoke onboarding with its value, or hand it to pending asks.
    try:
        if not message.from_user or message.chat.type != "private":
            raise PermissionError
        user = bot._resolve_user(str(message.chat.id), message)
        if not user or not user.get("user_id") or not user.get("tenant_id"):
            raise PermissionError
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


def collect_album(bot: TelegramBot, message: Message) -> None:
    """Hold album metadata until its caption arrives, before downloading any member.

    Secure enrollment supports individual files. A batch with a secure caption is
    refused as a whole, including captionless members arriving in this window.
    Tasks use the bot's existing shutdown registry.
    """
    key = (str(message.chat.id), "intake:" + str(message.media_group_id))
    pending = bot._album_buffers.get(key)
    if pending is None:
        pending = {"messages": [], "private": False}
        bot._album_buffers[key] = pending
        bot._album_tasks.setdefault(key, set()).add(asyncio.create_task(_flush_intake(bot, key)))
    pending["private"] = pending["private"] or is_secure(message.caption or "")
    if len(pending["messages"]) < 25:
        pending["messages"].append(message)
    else:
        pending["private"] = True  # refuse an unbounded or incomplete batch


async def _flush_intake(bot: TelegramBot, key: tuple[str, str]) -> None:
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
        for message in pending["messages"]:
            await bot.handle_file(message, _album_checked=True)
    except Exception:
        # No raw message/filename/exception leaves this boundary on failure.
        return
    finally:
        tasks = bot._album_tasks.get(key)
        if tasks is not None:
            tasks.discard(mine)
            if not tasks:
                bot._album_tasks.pop(key, None)
