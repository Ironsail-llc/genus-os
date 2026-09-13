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

    # Every row below carries a sub_id, passing rows included. Two findings
    # about two different settings must never share a row id: a dashboard or a
    # log filter keying on `telegram.token` would otherwise see one of them at
    # random depending on which branch was taken.

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
            results.append(
                Result(status="pass", detail=f"bot {head} answered getMe", sub_id="token")
            )
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
        results.append(Result(status="pass", detail=f"chat {chat}", sub_id="chat"))
    else:
        results.append(
            fail(
                f"ROBOTHOR_TELEGRAM_CHAT_ID={chat!r} is neither a numeric id nor an @name",
                sub_id="chat",
            )
        )
    return results


#: Slack's own prefixes. A bot token is ``xoxb-``; an app-level (Socket Mode)
#: token is ``xapp-``. They sit next to each other in the Slack console and are
#: swapped often enough that the shape check pays for itself.
_BOT_PREFIX = "xoxb-"
_APP_PREFIX = "xapp-"

#: Where a Slack delivery may be aimed. ``C``/``G``/``D`` a conversation,
#: ``U``/``W`` a person (the channel opens the DM). A ``#name`` is not an id and
#: the channel refuses it rather than walking ``conversations.list`` per send.
_TARGET_PREFIXES = ("C", "G", "D", "U", "W")


async def _slack(ctx: DoctorContext) -> list[Result]:
    """Whether Slack is configured consistently, if it is configured at all.

    Slack is an optional channel and its absence is the norm. Three settings
    can each be individually wrong in a way nothing notices until a briefing
    does not arrive, so each gets its OWN row: two findings about two different
    settings sharing one row id means a dashboard filtering on ``slack.token``
    sees one of them at random depending on which branch ran.

    Nothing here prints a token, malformed or not. A value in the bot-token
    slot is a credential whatever its shape.

    The tokens come from :func:`~robothor.engine.channels.slack_credentials.
    slack_credentials`, not from ``ctx.settings``. The settings layer never
    consults the vault, and the vault is where `genus channel add slack` writes
    by default -- so reading settings reported "not configured" for exactly the
    installs this check exists to police, and the swap check below could not run
    on any of them. The SOURCE is named in each passing row, because "which
    layer answered" is the question an operator has when two surfaces disagree.
    """
    from robothor.engine.channels.slack_credentials import slack_credentials

    found = slack_credentials()
    token = found.bot_token or ""
    app_token = found.app_token or ""
    target = ctx.settings.channels.slack_verify_target
    if not token and not app_token and not target:
        return [skip("not configured — Slack is an optional channel")]

    results: list[Result] = []
    if not token:
        results.append(
            fail(
                "Slack is partly configured but ROBOTHOR_SLACK_BOT_TOKEN is not set, "
                "so nothing can be delivered to Slack",
                sub_id="token",
            )
        )
    elif token.startswith(_APP_PREFIX):
        results.append(
            fail(
                "ROBOTHOR_SLACK_BOT_TOKEN holds an app-level token (xapp-). The bot "
                "token is the xoxb- value on the Slack app's OAuth page",
                sub_id="token",
            )
        )
    elif not token.startswith(_BOT_PREFIX):
        results.append(
            fail(
                "ROBOTHOR_SLACK_BOT_TOKEN does not start with 'xoxb-' — a bot token is "
                "expected here, not an app-level token",
                sub_id="token",
            )
        )
    else:
        results.append(
            Result(
                status="pass",
                detail=f"a bot token is configured ({found.bot_source})",
                sub_id="token",
            )
        )

    if not app_token:
        results.append(
            Result(
                status="skip",
                detail=(
                    "ROBOTHOR_SLACK_APP_TOKEN is unset: outbound delivery works, the "
                    "inbound Socket Mode bot will not start"
                ),
                sub_id="app_token",
            )
        )
    elif not app_token.startswith(_APP_PREFIX):
        results.append(
            fail(
                "ROBOTHOR_SLACK_APP_TOKEN does not start with 'xapp-' — Socket Mode "
                "needs the app-level token, not the bot token",
                sub_id="app_token",
            )
        )
    else:
        results.append(
            Result(
                status="pass",
                detail=f"an app-level token is configured ({found.app_source})",
                sub_id="app_token",
            )
        )

    if not target:
        results.append(
            Result(
                status="skip",
                detail=(
                    "ROBOTHOR_SLACK_VERIFY_TARGET is unset, so `genus channel verify "
                    "slack` has nothing to aim at"
                ),
                sub_id="target",
            )
        )
    elif target[0] in _TARGET_PREFIXES and target.isalnum():
        # The id itself is instance data and this row rides into
        # `GET /api/doctor`, which the bridge serves over HTTP and caches. A
        # failure names the bad value because that IS the finding; a pass has
        # nothing to add by naming it.
        results.append(
            Result(status="pass", detail="a verify target is configured", sub_id="target")
        )
    else:
        results.append(
            fail(
                f"ROBOTHOR_SLACK_VERIFY_TARGET={target!r} is not a Slack id. Use the "
                "conversation id (C…/G…/D…) or a user id (U…/W…); a #name cannot be "
                "posted to",
                sub_id="target",
            )
        )
    return results


async def _slack_verify(ctx: DoctorContext) -> Result:
    """Does the Slack token actually work, and does the app hold its scopes?

    The shape check above catches a token pasted from the wrong field. It
    cannot catch the two failures that account for the rest: a revoked token,
    and an app installed without ``chat:write`` — both of which look exactly
    like a correctly configured instance until a briefing does not arrive.

    Two read-only round trips, and both earn their place:

    ``auth.test`` tells a live token from a revoked one, which no amount of
    shape checking can.

    The scope probe (``conversations.list(limit=1)``) catches an app installed
    without the scopes the code calls. ``missing_scope`` is the single most
    common Slack setup failure and it is invisible until a send — so a
    ``slack.verify`` that skipped it would show green, here and on the bridge's
    Health panel, for an app that cannot post a thing. A first cut of this check
    did skip it; the scopes the error names are the whole fix, so they are
    surfaced and nothing else from the error is.

    Skipped under ``--offline``: this leaves the box. What the doctor does NOT
    do is post. That is ``genus channel verify slack``'s third step, and a
    diagnostic that writes into the operator's workspace every time a Health
    panel refreshes is not a diagnostic. Reading does not have that problem.
    """
    from robothor.engine.channels.slack_credentials import slack_credentials

    if not slack_credentials().can_send:
        return skip("no Slack bot token configured")
    if ctx.offline:
        return Result(status="skip", detail="--offline")

    from robothor.engine.channels.slack import SlackChannel

    channel = SlackChannel()
    try:
        report = await channel.health()
    except Exception as exc:  # noqa: BLE001 — a check must not raise on a broken box
        return skip(f"Slack could not be reached ({type(exc).__name__})")
    if not report.get("ok"):
        detail = str(report.get("error") or "auth.test did not succeed")
        return fail(f"Slack rejected the bot token: {detail}")

    missing = await channel.scope_probe()
    if missing is not None:
        return fail(f"the Slack app is missing a scope: {missing}")
    # No team name and no bot id: this row rides into `GET /api/doctor` on every
    # healthy refresh, and the workspace name is the operator's own data.
    return ok("auth.test and the scope probe both answered")


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
        title="Slack is configured consistently",
        category="channels",
        severity="info",
        run=_slack,
    ),
    Check(
        id="slack.verify",
        title="Slack accepts this instance's token",
        category="channels",
        severity="info",
        run=_slack_verify,
    ),
)
