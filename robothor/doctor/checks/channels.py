"""Delivery channels. All of them optional; a badly configured one is not.

Telegram is the case that matters. ``genus config validate`` used to demand a
bot token and a chat id, so every headless instance -- agents with
``delivery: none`` talking through CRM tasks and notifications, which is the
documented deploy -- failed two checks it could never pass, and the operator
learned that red output here is normal. Absent is information. Configured
wrongly is an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: A Telegram bot token is ``<digits>:<secret>``; five digits is the shortest
#: bot id Telegram has ever issued.
_TOKEN_PREFIX_DIGITS = 5

#: ``getMe`` is the only Bot API call that costs nothing and proves everything:
#: the token is real, unrevoked, and belongs to the bot the operator thinks.
_GET_ME = "https://api.telegram.org/bot{token}/getMe"


async def _telegram(ctx: DoctorContext) -> list[Result]:
    """The Telegram bot token and chat id are usable, if they are set at all.

    Nothing here is required: a Telegram-free instance is supported and common.
    A half-configured one is not -- a token with no chat id delivers nowhere,
    and a malformed token fails at the first send with an error nobody sees
    until a message is missed. When a token IS set and the run is not
    ``--offline``, ``getMe`` is called, which is the only way to tell a live
    token from a revoked one. The token itself is never printed; the digits
    before the colon are the bot's public id and identify WHICH bot delivers.
    """
    channels = ctx.settings.channels
    token = channels.telegram_bot_token
    chat = channels.telegram_chat_id
    if not token and not chat:
        return [skip("not configured — delivery=none is a supported deployment")]

    results: list[Result] = []
    head, _, tail = token.partition(":")
    token_shaped = bool(head.isdigit() and len(head) >= _TOKEN_PREFIX_DIGITS and tail)
    if not token:
        results.append(
            fail("a chat id is set but ROBOTHOR_TELEGRAM_BOT_TOKEN is not", sub_id="token")
        )
    elif not token_shaped:
        # The value is a credential even when it is malformed.
        results.append(
            fail(
                "ROBOTHOR_TELEGRAM_BOT_TOKEN is not shaped like a bot token "
                "(expected <digits>:<secret>)",
                sub_id="token",
            )
        )
    elif ctx.offline:
        results.append(Result(status="skip", detail=f"bot {head}; --offline", sub_id="token"))
    else:
        response = await ctx.run_blocking(ctx.fetch, _GET_ME.format(token=token))
        if response.ok:
            results.append(ok(f"bot {head} answered getMe"))
        elif response.status in (401, 404):
            results.append(
                fail(
                    f"bot {head}: Telegram rejected the token (HTTP {response.status})",
                    sub_id="token",
                )
            )
        else:
            reported = response.error or f"HTTP {response.status}"
            results.append(
                Result(
                    status="skip",
                    detail=f"bot {head}; getMe unreachable ({reported})",
                    sub_id="token",
                )
            )

    if not chat:
        results.append(
            fail("a bot token is set but ROBOTHOR_TELEGRAM_CHAT_ID is not", sub_id="chat")
        )
    elif chat.startswith("@") or chat.lstrip("-").isdigit():
        results.append(ok(f"chat {chat}"))
    else:
        results.append(
            fail(
                f"ROBOTHOR_TELEGRAM_CHAT_ID={chat!r} is neither a numeric id nor an @name",
                sub_id="chat",
            )
        )
    return results


async def _slack(ctx: DoctorContext) -> Result:
    """Whether a Slack bot token is configured. Informational only.

    Slack is an optional channel and its absence is the norm. The shape is
    checked because a value pasted from the wrong field of the Slack console
    (an app token where a bot token belongs) is invisible until the first send.
    """
    channels = ctx.settings.channels
    token = channels.slack_bot_token
    if not token:
        return skip("no Slack bot token configured")
    if not token.startswith("xoxb-"):
        return fail(
            "ROBOTHOR_SLACK_BOT_TOKEN does not start with 'xoxb-' — a bot token is "
            "expected here, not an app-level token"
        )
    return ok("a Slack bot token is configured")


CHECKS: tuple[Check, ...] = (
    Check(
        id="telegram.token",
        title="Telegram is configured consistently",
        category="channels",
        severity="recommended",
        run=_telegram,
    ),
    Check(
        id="slack.token",
        title="Slack bot token",
        category="channels",
        severity="info",
        run=_slack,
    ),
)
