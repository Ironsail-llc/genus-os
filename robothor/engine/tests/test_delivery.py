"""Tests for delivery module — unexpanded env var guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from robothor.engine.delivery import _deliver_telegram, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


@pytest.fixture(autouse=True)
def _register_mock_sender():
    """Register a mock Telegram sender for all tests."""
    sender = AsyncMock()
    set_telegram_sender(sender)
    yield sender
    set_telegram_sender(None)


def _make_run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "test",
        "status": RunStatus.COMPLETED,
        "output_text": "Hello",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _make_config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "test",
        "name": "Test",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "12345",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


class TestUnexpandedEnvVarGuard:
    @pytest.mark.asyncio
    async def test_unexpanded_var_rejected(self, _register_mock_sender):
        """delivery_to containing ${...} is rejected before sending."""
        config = _make_config(delivery_to="${ROBOTHOR_TELEGRAM_CHAT_ID}")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is False
        _register_mock_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_chat_id_rejected(self, _register_mock_sender):
        """Empty delivery_to is rejected."""
        config = _make_config(delivery_to="")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is False
        _register_mock_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_chat_id_accepted(self, _register_mock_sender):
        """Valid numeric chat_id proceeds to send."""
        config = _make_config(delivery_to="7636850023")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is True
        _register_mock_sender.assert_called_once()
