"""Telegram progress mode: one message edited in place (stub)."""

from __future__ import annotations

from typing import Any


class TelegramProgress:
    def __init__(self, bot: Any, chat_id: str, message_id: int, **_kwargs: Any) -> None:
        self.bot = bot

    async def on_content(self, cumulative: str) -> None:
        raise NotImplementedError

    async def on_event(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    async def finalise(self, text: str) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
