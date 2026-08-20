"""Tests for the shared backing-service HTTP client (tools/service_client.py).

Engine tools that call local backing services (vision, bridge, voice) used to
construct ad-hoc ``httpx.AsyncClient``s with no error mapping: a stopped
service surfaced to the LLM as ``ConnectError: All connection attempts
failed`` plus a 224-line traceback in journald, and internal loopback URLs
leaked into agent context via ``HTTPStatusError`` messages.

``call_service`` maps transport/HTTP failures to short structured errors that
never echo the URL, logs one warning line per failure, and short-circuits
repeat dials to a dead service through a per-service circuit breaker.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from robothor.engine.tools import service_client
from robothor.engine.tools.service_client import (
    BREAKER_COOLDOWN_SECONDS,
    BREAKER_THRESHOLD,
    call_service,
    reset_circuit_breakers,
)

URL = "http://127.0.0.1:8600/health"


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


def _client_raising(exc: Exception) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.request = AsyncMock(side_effect=exc)
    return client


def _client_responding(status_code: int, json_data: dict[str, Any] | None = None) -> AsyncMock:
    resp = httpx.Response(
        status_code=status_code,
        json=json_data if json_data is not None else {},
        request=httpx.Request("GET", URL),
    )
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.request = AsyncMock(return_value=resp)
    return client


def _patched(client: AsyncMock):
    return patch.object(service_client.httpx, "AsyncClient", return_value=client)


# ── Error mapping ───────────────────────────────────────────────────────


async def test_connect_error_maps_to_offline():
    with _patched(_client_raising(httpx.ConnectError("All connection attempts failed"))):
        result = await call_service("vision", "GET", URL)
    assert result == {"error": "vision service offline", "service": "vision", "retryable": False}


async def test_connect_timeout_maps_to_offline():
    with _patched(_client_raising(httpx.ConnectTimeout("timed out"))):
        result = await call_service("vision", "GET", URL)
    assert result["error"] == "vision service offline"
    assert result["retryable"] is False


async def test_read_timeout_maps_to_timed_out_retryable():
    with _patched(_client_raising(httpx.ReadTimeout("read timed out"))):
        result = await call_service("bridge", "POST", URL, json={"a": 1})
    assert result == {"error": "bridge service timed out", "service": "bridge", "retryable": True}


async def test_5xx_maps_to_unavailable_with_code():
    with _patched(_client_responding(503)):
        result = await call_service("vision", "GET", URL)
    assert result == {
        "error": "vision service unavailable (HTTP 503)",
        "service": "vision",
        "retryable": True,
    }


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_rejection_maps_to_credentials_message_without_url(code: int):
    with _patched(_client_responding(code)):
        result = await call_service("bridge", "POST", URL)
    assert result["error"] == "bridge rejected engine credentials — check service auth config"
    assert result["retryable"] is False
    assert "127.0.0.1" not in str(result)


async def test_error_payloads_never_echo_url():
    for client in (
        _client_raising(httpx.ConnectError(f"failed to connect to {URL}")),
        _client_responding(500),
        _client_responding(404),
    ):
        reset_circuit_breakers()
        with _patched(client):
            result = await call_service("vision", "GET", URL)
        assert "error" in result
        assert "127.0.0.1" not in str(result)
        assert "8600" not in str(result)


async def test_success_returns_parsed_json():
    with _patched(_client_responding(200, {"ok": True, "mode": "basic"})):
        result = await call_service("vision", "GET", URL)
    assert result == {"ok": True, "mode": "basic"}


async def test_failure_logs_one_warning_no_traceback(caplog: pytest.LogCaptureFixture):
    with (
        caplog.at_level(logging.WARNING, logger="robothor.engine.tools.service_client"),
        _patched(_client_raising(httpx.ConnectError("boom"))),
    ):
        await call_service("vision", "GET", URL)
    records = [r for r in caplog.records if r.name == "robothor.engine.tools.service_client"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None


# ── Circuit breaker ─────────────────────────────────────────────────────


async def test_breaker_opens_after_threshold_and_skips_dial():
    client = _client_raising(httpx.ConnectError("down"))
    with _patched(client):
        for _ in range(BREAKER_THRESHOLD):
            result = await call_service("vision", "GET", URL)
        assert client.request.await_count == BREAKER_THRESHOLD
        # Breaker is now open: the next call must NOT dial.
        result = await call_service("vision", "GET", URL)
    assert client.request.await_count == BREAKER_THRESHOLD
    assert result["error"] == "vision service offline"
    assert result.get("circuit_open") is True


async def test_breaker_is_per_service():
    client = _client_raising(httpx.ConnectError("down"))
    with _patched(client):
        for _ in range(BREAKER_THRESHOLD):
            await call_service("vision", "GET", URL)
    ok_client = _client_responding(200, {"ok": True})
    with _patched(ok_client):
        result = await call_service("bridge", "POST", URL)
    assert result == {"ok": True}
    assert ok_client.request.await_count == 1


async def test_breaker_half_opens_after_cooldown():
    client = _client_raising(httpx.ConnectError("down"))
    with _patched(client):
        for _ in range(BREAKER_THRESHOLD):
            await call_service("vision", "GET", URL)
    # Age the breaker past the cooldown window.
    state = service_client._breakers["vision"]
    state.opened_at -= BREAKER_COOLDOWN_SECONDS + 1
    ok_client = _client_responding(200, {"ok": True})
    with _patched(ok_client):
        result = await call_service("vision", "GET", URL)
    assert result == {"ok": True}
    assert ok_client.request.await_count == 1


async def test_breaker_resets_on_success():
    fail = _client_raising(httpx.ConnectError("down"))
    with _patched(fail):
        for _ in range(BREAKER_THRESHOLD - 1):
            await call_service("vision", "GET", URL)
    with _patched(_client_responding(200, {"ok": True})):
        await call_service("vision", "GET", URL)
    # Two more failures: without the reset this would trip the breaker.
    fail2 = _client_raising(httpx.ConnectError("down"))
    with _patched(fail2):
        await call_service("vision", "GET", URL)
        await call_service("vision", "GET", URL)
        result = await call_service("vision", "GET", URL)
        assert fail2.request.await_count == 3  # third dial still attempted
    assert result.get("circuit_open") is None or fail2.request.await_count == 3


async def test_read_timeout_does_not_trip_breaker():
    """Timeouts mean the service is up but slow — the breaker must not
    short-circuit it as offline."""
    client = _client_raising(httpx.ReadTimeout("slow"))
    with _patched(client):
        for _ in range(BREAKER_THRESHOLD + 1):
            result = await call_service("vision", "GET", URL)
    assert client.request.await_count == BREAKER_THRESHOLD + 1
    assert result["error"] == "vision service timed out"
