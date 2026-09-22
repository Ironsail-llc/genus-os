"""A provider can commit a write before its response becomes unusable."""

from unittest.mock import AsyncMock

import httpx
import pytest

from robothor.engine.tools import service_client


@pytest.mark.parametrize("failure", ["timeout", "server_error", "invalid_json"])
async def test_committed_write_with_lost_response_is_not_marked_retryable(monkeypatch, failure):
    effects = []

    async def invoke(method, url, **kwargs):
        effects.append(kwargs["json"])
        request = httpx.Request(method, url)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic response lost", request=request)
        return httpx.Response(
            500 if failure == "server_error" else 200,
            content=b"response unavailable",
            request=request,
        )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.request.side_effect = invoke
    monkeypatch.setattr(service_client.httpx, "AsyncClient", lambda **kwargs: client)
    service_client.reset_circuit_breakers()
    try:
        result = await service_client.call_service(
            "synthetic-write", "POST", "https://synthetic.invalid/action", json={"item": "once"}
        )
    finally:
        service_client.reset_circuit_breakers()
    assert effects == [{"item": "once"}]
    assert result.get("error")
    assert result["retryable"] is False
    assert result["outcome_unknown"] is True


async def test_handler_write_before_transport_failure_is_uncertain(monkeypatch):
    from robothor.engine.tools import dispatch

    effects = []

    async def handler(args, ctx):
        effects.append(args)
        raise httpx.ReadTimeout("synthetic response lost")

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    result = await dispatch._execute_tool("create_note", {"body": "once"}, user_role="service")
    assert effects == [{"body": "once"}]
    assert result["retryable"] is False
    assert result["outcome_unknown"] is True


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_read_timeout_remains_retryable(monkeypatch, method):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.request.side_effect = httpx.ReadTimeout("synthetic response lost")
    monkeypatch.setattr(service_client.httpx, "AsyncClient", lambda **kwargs: client)
    service_client.reset_circuit_breakers()
    result = await service_client.call_service(
        "synthetic-read", method, "https://synthetic.invalid"
    )
    assert result["retryable"] is True
    assert not result.get("outcome_unknown")
