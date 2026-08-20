"""Vision tools degrade gracefully when the vision service is offline.

The operator can stop the vision service deliberately (e.g. thermal issues).
Vision tool calls must then return a short structured "vision service offline"
error — never a raw ConnectError traceback — and, when the persisted vision
mode file says the operator disabled vision, say exactly that.

The mode file convention mirrors robothor/vision/service.py: STATE_DIR (or
ROBOTHOR_MEMORY_DIR) / "vision_mode.txt". A 'disabled' mode value is written
by a newer vision service; both its presence and absence are tolerated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.vision import HANDLERS
from robothor.engine.tools.service_client import reset_circuit_breakers

CTX = ToolContext(agent_id="test", tenant_id="test-tenant")


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    return tmp_path


def _dead_client() -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.request = AsyncMock(side_effect=httpx.ConnectError("All connection attempts failed"))
    return client


def _patch_dead():
    from robothor.engine.tools import service_client

    return patch.object(service_client.httpx, "AsyncClient", return_value=_dead_client())


@pytest.mark.parametrize("tool", ["look", "who_is_here", "list_enrolled_faces"])
async def test_offline_returns_structured_error_not_crash(tool: str):
    with _patch_dead():
        result = await HANDLERS[tool]({"prompt": "what do you see"}, CTX)
    assert result["error"] == "vision service offline"
    assert result["service"] == "vision"
    assert result["retryable"] is False
    assert "ConnectError" not in str(result)
    assert "127.0.0.1" not in str(result)


async def test_offline_with_disabled_mode_file_reports_operator_disabled(_state_dir):
    (_state_dir / "vision_mode.txt").write_text("disabled")
    with _patch_dead():
        result = await HANDLERS["who_is_here"]({}, CTX)
    assert result == {
        "available": False,
        "mode": "disabled",
        "reason": "vision disabled by operator",
    }


async def test_offline_with_other_mode_file_returns_offline_error(_state_dir):
    (_state_dir / "vision_mode.txt").write_text("basic")
    with _patch_dead():
        result = await HANDLERS["look"]({}, CTX)
    assert result["error"] == "vision service offline"


async def test_offline_without_mode_file_returns_offline_error(_state_dir):
    assert not (_state_dir / "vision_mode.txt").exists()
    with _patch_dead():
        result = await HANDLERS["who_is_here"]({}, CTX)
    assert result["error"] == "vision service offline"


async def test_repeated_offline_calls_short_circuit_via_breaker():
    from robothor.engine.tools import service_client

    client = _dead_client()
    with patch.object(service_client.httpx, "AsyncClient", return_value=client):
        for _ in range(service_client.BREAKER_THRESHOLD + 2):
            result = await HANDLERS["who_is_here"]({}, CTX)
    assert client.request.await_count == service_client.BREAKER_THRESHOLD
    assert result["error"] == "vision service offline"
