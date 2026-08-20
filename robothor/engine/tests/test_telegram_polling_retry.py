"""Boot-time network failures must not kill the Telegram polling task.

Observed twice in production (Aug 17 + Aug 19): a DNS failure during the
first ``getUpdates`` propagated out of ``start_polling``, the daemon's
FIRST_COMPLETED wait treated the completed task as a shutdown trigger,
and the whole engine restarted — with exit 0, so OnFailure never paged.

``start_polling`` now retries ``dp.start_polling`` on
``TelegramNetworkError`` with bounded backoff (5s doubling to a 60s cap,
indefinitely): a Telegram outage must not take down the scheduler,
health API, or watchdog.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError

from robothor.engine.telegram import TelegramBot


def _make_bot() -> TelegramBot:
    """Build a TelegramBot instance without touching the network or DB."""
    bot = object.__new__(TelegramBot)
    bot.config = MagicMock(bot_token="123:abc")
    bot._load_persisted_history = MagicMock()
    bot.bot = MagicMock()
    bot.bot.set_my_commands = AsyncMock()
    bot.dp = MagicMock()
    bot.dp.resolve_used_update_types = MagicMock(return_value=["message"])
    return bot


def _network_error(msg: str = "getaddrinfo failed") -> TelegramNetworkError:
    return TelegramNetworkError(method=MagicMock(), message=msg)


@pytest.mark.asyncio
async def test_network_error_is_retried_with_backoff(monkeypatch):
    """Two DNS failures then success: polling survives, with 5s/10s waits."""
    bot = _make_bot()
    bot.dp.start_polling = AsyncMock(side_effect=[_network_error(), _network_error(), None])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("robothor.engine.telegram.asyncio.sleep", fake_sleep)

    await bot.start_polling()  # must NOT raise

    assert bot.dp.start_polling.await_count == 3
    assert sleeps == [5.0, 10.0], "backoff must start at 5s and double"


@pytest.mark.asyncio
async def test_backoff_is_capped_at_60s(monkeypatch):
    failures = [_network_error() for _ in range(8)] + [None]
    bot = _make_bot()
    bot.dp.start_polling = AsyncMock(side_effect=failures)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("robothor.engine.telegram.asyncio.sleep", fake_sleep)

    await bot.start_polling()

    assert bot.dp.start_polling.await_count == 9
    assert max(sleeps) == 60.0
    assert sleeps[:4] == [5.0, 10.0, 20.0, 40.0]
    assert sleeps[4:] == [60.0] * 4, "backoff must cap at 60s, not grow unbounded"


@pytest.mark.asyncio
async def test_non_network_error_still_propagates(monkeypatch):
    """Only network errors are retried — a programming error must surface."""
    bot = _make_bot()
    bot.dp.start_polling = AsyncMock(side_effect=ValueError("boom"))

    async def fake_sleep(seconds: float) -> None:  # pragma: no cover - defensive
        pass

    monkeypatch.setattr("robothor.engine.telegram.asyncio.sleep", fake_sleep)

    with pytest.raises(ValueError, match="boom"):
        await bot.start_polling()


@pytest.mark.asyncio
async def test_clean_return_stops_polling_loop():
    """aiogram returning normally (SIGTERM stop) must end start_polling."""
    bot = _make_bot()
    bot.dp.start_polling = AsyncMock(return_value=None)

    await bot.start_polling()

    assert bot.dp.start_polling.await_count == 1
