"""Slack bot — Socket Mode adapter for agent delivery.

Uses slack-bolt async SDK. Started by daemon.py alongside TelegramBot
when ROBOTHOR_SLACK_BOT_TOKEN and ROBOTHOR_SLACK_APP_TOKEN are set.

Shares sessions with Telegram and web chat via the existing session system.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from robothor.engine.channels.slack_credentials import (
    APP_TOKEN_ENV,
    BOT_TOKEN_ENV,
    slack_credentials,
)
from robothor.engine.chunking import split_message

logger = logging.getLogger(__name__)

# Maximum message length for Slack
MAX_SLACK_LENGTH = 4000


class SlackBot:
    """Slack bot using Socket Mode (no public webhook needed)."""

    def __init__(self, runner: Any, config: Any) -> None:
        self.runner = runner
        self.config = config
        self._app = None
        self._handler = None
        self._started = False

    @staticmethod
    def _allowed_users() -> set[str]:
        return {
            u.strip()
            for u in os.environ.get("ROBOTHOR_SLACK_ALLOWED_USERS", "").split(",")
            if u.strip()
        }

    @staticmethod
    def _allowed_channels() -> set[str]:
        return {
            c.strip()
            for c in os.environ.get("ROBOTHOR_SLACK_ALLOWED_CHANNELS", "").split(",")
            if c.strip()
        }

    def _authorized(self, user_id: str, channel: str) -> bool:
        """Inbound authorization for Slack commands.

        When neither allowlist is configured, allow (the bot was deliberately
        activated; a startup warning already flagged the open surface). When
        either is set, the message must match a listed user OR channel.
        """
        users, channels = self._allowed_users(), self._allowed_channels()
        if not users and not channels:
            return True
        return user_id in users or channel in channels

    async def start(self) -> None:
        """Initialize and start the Slack bot."""
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            logger.error("slack-bolt not installed. Install with: pip install slack-bolt")
            return

        # Through the one credential reader, not os.environ: `genus channel add
        # slack` writes to the VAULT by default, and this gate read an
        # environment nothing ever preloads those rows into — so the bot
        # silently never started on the most common install while `verify`
        # reported Socket Mode green.
        found = slack_credentials(live=True)
        if not found.can_listen:
            logger.warning(
                "%s or %s is set neither in the environment nor in this instance's "
                "vault, so the Slack bot is not starting.",
                BOT_TOKEN_ENV,
                APP_TOKEN_ENV,
            )
            return
        bot_token, app_token = found.bot_token, found.app_token

        if not self._allowed_users() and not self._allowed_channels():
            logger.warning(
                "Slack bot has no ROBOTHOR_SLACK_ALLOWED_USERS/CHANNELS allowlist — "
                "ANY user in a joined workspace/channel can drive the main agent. "
                "Set an allowlist to restrict inbound commands."
            )

        app = AsyncApp(token=bot_token)
        self._app = app

        # Register message handler
        @app.event("message")  # type: ignore[untyped-decorator]
        async def handle_message(event: dict[str, Any], say: Any) -> None:
            await self._on_message(event, say)

        # Register slash commands
        @app.command("/status")  # type: ignore[untyped-decorator]
        async def handle_status(ack: Any, respond: Any) -> None:
            await ack()
            await respond("Engine status: running")

        @app.command("/clear")  # type: ignore[untyped-decorator]
        async def handle_clear(ack: Any, respond: Any, command: dict[str, Any]) -> None:
            await ack()
            from robothor.engine.chat import get_shared_session

            channel = command.get("channel_id", "")
            session_key = f"agent:main:slack:{channel}"
            session = get_shared_session(session_key)
            session.history.clear()
            await respond("Session cleared.")

        # Register platform sender for delivery
        from robothor.engine.delivery import register_platform_sender

        register_platform_sender("slack", self.slack_send, chunk_size=MAX_SLACK_LENGTH)

        # Start Socket Mode handler
        handler = AsyncSocketModeHandler(app, app_token)
        self._handler = handler
        await handler.start_async()
        self._started = True
        logger.info("Slack bot started (Socket Mode)")

    async def slack_send(self, channel_id: str, text: str) -> list[Any]:
        """Post to Slack and RETURN what landed, one entry per chunk.

        A method, not a closure inside :meth:`start`, because a closure there is
        unreachable from a test: the only test that called ``start()`` returned
        before the definition, which is how this spent its whole life returning
        ``None`` with nobody noticing.

        Three properties decide whether an operator is told the truth about a
        Slack briefing:

        * **The return value is the only evidence anything landed.** Returning
          ``None`` — what this did before ``delivery.channel: slack`` could
          resolve — would record every successful briefing as
          ``failed:slack_send`` and page the operator about a message that
          arrived.
        * **A failure partway through keeps what landed.** ``chat_postMessage``
          raises ``SlackApiError``; letting it propagate turned a
          ``partial:2/3`` into a total failure and lost the ids of the two
          messages the operator can actually see. Each chunk is attempted and
          the failures are logged, exactly as ``TelegramBot.send_message`` does.
        * **It splits with** :func:`~robothor.engine.chunking.split_message`,
          the same function the shim counts with (it is registered with
          ``chunk_size=MAX_SLACK_LENGTH``). Two splitters would disagree about
          how many messages a body becomes, and a truncated send would read as
          a complete one.

        Returns:
            The ``SlackResponse`` for each chunk that posted, in send order.
            Empty when nothing did. Slack's per-message id is ``ts``, which
            :func:`~robothor.engine.channels.base.acknowledged_messages` reads
            into ``platform_ids`` so ``channel_bus`` can map a reply back.
        """
        landed: list[Any] = []
        if not (self._app and self._app.client):
            logger.warning("Slack send requested before the app was started")
            return landed

        for chunk in _split_text(text, MAX_SLACK_LENGTH):
            try:
                landed.append(
                    await self._app.client.chat_postMessage(
                        channel=channel_id,
                        text=chunk,
                        mrkdwn=True,
                    )
                )
            except Exception as e:  # noqa: BLE001 — one bad chunk is not a lost send
                logger.error("Slack chunk rejected for %s: %s", channel_id, e)
        return landed

    async def stop(self) -> None:
        """Stop the Slack bot."""
        if self._handler and self._started:
            await self._handler.close_async()
            self._started = False
            logger.info("Slack bot stopped")

    async def _on_message(self, event: dict[str, Any], say: Any) -> None:
        """Handle incoming Slack messages."""
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        channel = event.get("channel", "")
        user_id = event.get("user", "")

        if not self._authorized(user_id, channel):
            logger.warning(
                "Ignoring Slack message from unauthorized user %s (channel %s)",
                user_id,
                channel,
            )
            return

        logger.info(
            "Slack message from %s (channel %s): %s",
            user_id,
            channel,
            text[:100],
        )

        # Resolve user → tenant (Slack user table not yet implemented;
        # fall back to engine config tenant for now)
        tenant_id = self.config.tenant_id

        # Use shared session system
        from robothor.engine.chat import get_shared_session

        session_key = f"agent:main:slack:{channel}"
        session = get_shared_session(session_key)

        # Run agent
        try:
            from robothor.engine.models import TriggerType

            run = await self.runner.execute(
                agent_id="main",
                message=text,
                trigger_type=TriggerType.SLACK,
                tenant_id=tenant_id,
                user_id=f"slack:{user_id}",
                user_role="user",
                conversation_history=list(session.history) if session.history else None,
            )

            if run.output_text:
                chunks = _split_text(run.output_text, MAX_SLACK_LENGTH)
                for chunk in chunks:
                    await say(text=chunk, mrkdwn=True)
            else:
                await say("I processed your request but have no output to share.")

        except Exception:
            logger.exception("Slack agent execution failed")
            await say("Something went wrong. Please try again.")


def _split_text(text: str, max_length: int) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Delegates to the shared splitter. It had its own near-copy, which is fine
    while nothing counts the chunks and wrong the moment something does: the
    shim measures a body with ``split_message`` to decide whether every chunk
    was acknowledged, and two splitters that disagree by one turn a truncated
    briefing into a delivered one.
    """
    return split_message(text, max_length)


def is_slack_configured() -> bool:
    """Whether the INBOUND half can start: both tokens, from wherever they live.

    Resolved through :func:`slack_credentials`, so this answers the same way as
    the daemon gate, the outbound channel and the doctor. It used to read
    ``os.environ`` and disagree with all three on any vault install.
    """
    return slack_credentials().can_listen
