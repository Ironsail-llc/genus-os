"""Test fixtures for the Robothor TUI."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.tui.client import EngineClient


@pytest.fixture
def mock_client():
    """A mocked EngineClient for unit tests."""
    client = MagicMock(spec=EngineClient)
    client.base_url = "http://127.0.0.1:18800"
    client.session_key = "agent:main:tui-test"
    client.check_health = AsyncMock(
        return_value={
            "status": "healthy",
            "engine_version": "0.1.0",
            "tenant_id": "test-tenant",
            "bot_configured": False,
            "agents": {
                "email-classifier": {
                    "enabled": True,
                    "last_status": "completed",
                    "consecutive_errors": 0,
                },
                "supervisor": {
                    "enabled": True,
                    "last_status": "completed",
                    "consecutive_errors": 0,
                },
            },
        }
    )
    client.get_history = AsyncMock(return_value=[])
    client.abort = AsyncMock(return_value=True)
    client.clear = AsyncMock(return_value=True)
    client.get_runs = AsyncMock(return_value=[])
    client.get_costs = AsyncMock(
        return_value={
            "hours": 24,
            "total_runs": 10,
            "total_cost_usd": 0.05,
            "agents": {},
        }
    )
    client.close = AsyncMock()
    return client
