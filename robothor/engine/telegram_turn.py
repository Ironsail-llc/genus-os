"""What one Telegram turn carries into a run.

Lifted out of the handler and the interactive runner: how a "Reply to" quote
joins the text, and whose turn it is, are questions about the turn itself.
"""

from __future__ import annotations

from typing import Any


async def with_reply_quote(bot: Any, chat_id: str, message: Any, user_text: str) -> str:
    """``user_text``, with a compact quote of the fleet message it replies to.

    If this message is a Telegram "Reply to", look up the original in
    channel_message_map. When it was a fleet surface (e.g. a DevOps report),
    prepend a compact quote so main sees the thread context inline, and
    remember the linkage so the user turn's JSONB records the reference.
    """
    if not (message.reply_to_message and message.reply_to_message.message_id):
        return user_text
    from robothor.engine.channel_bus import format_reply_prefix, resolve_reply_context_async

    reply_ctx = await resolve_reply_context_async(
        chat_id=chat_id,
        platform_message_id=str(message.reply_to_message.message_id),
        tenant_id=bot._get_tenant_id(chat_id),
    )
    if not reply_ctx:
        return user_text
    bot._reply_context_buffers[chat_id] = reply_ctx
    return f"{format_reply_prefix(reply_ctx)}\n\n{user_text}"


def run_sender(
    bot: Any, chat_id: str, sender_info: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str, str]:
    """The sender dict, ``trigger_detail`` and tenant for this turn's run.

    Prefers the sender info captured for THIS message in the handler, before
    any async gap, over the shared chat_id-keyed cache; a caller that bypasses
    the coalescing buffer (no ``sender_info``) falls back to the cache.
    """
    user = sender_info if sender_info is not None else bot._chat_user_info.get(chat_id)
    sender = user["display_name"] if user else ""
    detail = f"chat:{chat_id}"
    if sender:
        detail += "|sender:" + sender.replace("|", "")
    tenant = (user.get("tenant_id") if user else None) or bot._get_tenant_id(chat_id)
    return user, detail, tenant
