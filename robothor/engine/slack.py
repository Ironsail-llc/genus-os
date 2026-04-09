"""Slack bot — Socket Mode adapter for agent delivery.

Uses slack-bolt async SDK. Started by daemon.py alongside TelegramBot
when ROBOTHOR_SLACK_BOT_TOKEN and ROBOTHOR_SLACK_APP_TOKEN are set.

Shares sessions with Telegram and web chat via the existing session system.
"""

from __future__ import annotations

import logging
import os
from typing import Any

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

    async def start(self) -> None:
        """Initialize and start the Slack bot."""
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            logger.error("slack-bolt not installed. Install with: pip install slack-bolt")
            return

        bot_token = os.environ.get("ROBOTHOR_SLACK_BOT_TOKEN")
        app_token = os.environ.get("ROBOTHOR_SLACK_APP_TOKEN")

        if not bot_token or not app_token:
            logger.warning(
                "ROBOTHOR_SLACK_BOT_TOKEN or ROBOTHOR_SLACK_APP_TOKEN not set, "
                "Slack bot not starting."
            )
            return

        self._app = AsyncApp(token=bot_token)

        # Register message handler
        @self._app.event("message")
        async def handle_message(event: dict, say: Any) -> None:
            await self._on_message(event, say)

        # Register slash commands
        @self._app.command("/status")
        async def handle_status(ack: Any, respond: Any) -> None:
            await ack()
            await respond("Engine status: running")

        @self._app.command("/clear")
        async def handle_clear(ack: Any, respond: Any, command: dict) -> None:
            await ack()
            from robothor.engine.chat import get_shared_session

            channel = command.get("channel_id", "")
            session_key = f"agent:main:slack:{channel}"
            session = get_shared_session(session_key)
            session.history.clear()
            await respond("Session cleared.")

        # Register platform sender for delivery
        from robothor.engine.delivery import register_platform_sender

        async def slack_send(channel_id: str, text: str) -> None:
            if self._app and self._app.client:
                # Split long messages
                chunks = _split_text(text, MAX_SLACK_LENGTH)
                for chunk in chunks:
                    await self._app.client.chat_postMessage(
                        channel=channel_id,
                        text=chunk,
                        mrkdwn=True,
                    )

        register_platform_sender("slack", slack_send)

        # Start Socket Mode handler
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        await self._handler.start_async()
        self._started = True
        logger.info("Slack bot started (Socket Mode)")

    async def stop(self) -> None:
        """Stop the Slack bot."""
        if self._handler and self._started:
            await self._handler.close_async()
            self._started = False
            logger.info("Slack bot stopped")

    async def _on_message(self, event: dict, say: Any) -> None:
        """Handle incoming Slack messages."""
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        channel = event.get("channel", "")
        user_id = event.get("user", "")

        logger.info(
            "Slack message from %s (channel %s): %s",
            user_id,
            channel,
            text[:100],
        )

        # Resolve user → tenant
        from robothor.engine.users import lookup_user

        user_info = lookup_user(f"slack:{user_id}")
        tenant_id = user_info["tenant_id"] if user_info else self.config.tenant_id

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
                trigger_type=TriggerType.WEBCHAT,  # reuse webchat trigger
                tenant_id=tenant_id,
                session=session,
            )

            if run.output_text:
                chunks = _split_text(run.output_text, MAX_SLACK_LENGTH)
                for chunk in chunks:
                    await say(text=chunk, mrkdwn=True)
            else:
                await say("I processed your request but have no output to share.")

        except Exception as e:
            logger.error("Slack agent execution failed: %s", e)
            await say(f"Something went wrong: {e}")


def _split_text(text: str, max_length: int) -> list[str]:
    """Split text into chunks that fit within Slack's message limit."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def is_slack_configured() -> bool:
    """Check if Slack environment variables are set."""
    return bool(
        os.environ.get("ROBOTHOR_SLACK_BOT_TOKEN") and os.environ.get("ROBOTHOR_SLACK_APP_TOKEN")
    )
