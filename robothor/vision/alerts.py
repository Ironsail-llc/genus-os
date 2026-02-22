"""
Alert system for vision events — pluggable notification backends.

Replaces hardcoded Telegram alerts with a pluggable interface.
Users can register custom alert handlers for different event types.

Usage:
    from robothor.vision.alerts import AlertManager, TelegramAlert

    manager = AlertManager()
    manager.add_handler(TelegramAlert(bot_token="...", chat_id="..."))
    await manager.send("unknown_person", image_bytes=jpeg, message="Unknown person detected")

    # Or use the webhook handler for generic HTTP notifications
    manager.add_handler(WebhookAlert(url="https://example.com/webhook"))
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class AlertHandler(ABC):
    """Base class for alert handlers."""

    @abstractmethod
    async def send(
        self,
        event_type: str,
        message: str,
        image_bytes: bytes | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """Send an alert.

        Args:
            event_type: Type of event (unknown_person, motion, departure, etc.).
            message: Human-readable alert message.
            image_bytes: Optional JPEG image data.
            metadata: Optional extra data about the event.

        Returns:
            True if sent successfully.
        """


class TelegramAlert(AlertHandler):
    """Send alerts via Telegram Bot API."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    async def send(
        self,
        event_type: str,
        message: str,
        image_bytes: bytes | None = None,
        metadata: dict | None = None,
    ) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram alert skipped: missing bot_token or chat_id")
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if image_bytes:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                        data={"chat_id": self.chat_id, "caption": message[:1024]},
                        files={"photo": ("alert.jpg", image_bytes, "image/jpeg")},
                    )
                else:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                        json={"chat_id": self.chat_id, "text": message},
                    )
                return resp.status_code == 200
        except Exception as e:
            logger.error("Telegram alert failed: %s", e)
            return False


class WebhookAlert(AlertHandler):
    """Send alerts via generic HTTP webhook."""

    def __init__(self, url: str, headers: dict | None = None):
        self.url = url
        self.headers = headers or {}

    async def send(
        self,
        event_type: str,
        message: str,
        image_bytes: bytes | None = None,
        metadata: dict | None = None,
    ) -> bool:
        try:
            payload = {
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.url,
                    json=payload,
                    headers=self.headers,
                )
                return 200 <= resp.status_code < 300
        except Exception as e:
            logger.error("Webhook alert failed: %s", e)
            return False


class AlertManager:
    """Manages multiple alert handlers and dispatches events."""

    def __init__(self) -> None:
        self.handlers: list[AlertHandler] = []

    def add_handler(self, handler: AlertHandler) -> None:
        """Register an alert handler."""
        self.handlers.append(handler)

    async def send(
        self,
        event_type: str,
        message: str,
        image_bytes: bytes | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Send alert to all registered handlers.

        Returns number of successful deliveries.
        """
        success_count = 0
        for handler in self.handlers:
            try:
                ok = await handler.send(event_type, message, image_bytes, metadata)
                if ok:
                    success_count += 1
            except Exception as e:
                logger.error("Alert handler %s failed: %s", type(handler).__name__, e)
        return success_count
