"""
Tests for bridge_service.py endpoints.

All CRM operations are mocked via crm_dal patches.
External HTTP calls (Memory) are mocked via mock_http_client fixture.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Ensure bridge source is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Health Endpoint ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_all_services_ok(test_client, mock_http_client):
    """When CRM and memory are healthy, health reports status: ok."""
    mock_http_client.get = AsyncMock(
        return_value=MagicMock(spec=httpx.Response, status_code=200)
    )

    with patch("crm_dal.check_health", return_value={"status": "ok"}):
        r = await test_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["services"]["crm"] == "ok"
        assert data["services"]["memory"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded_when_crm_down(test_client, mock_http_client):
    """When CRM check fails, health reports degraded."""
    mock_http_client.get = AsyncMock(
        return_value=MagicMock(spec=httpx.Response, status_code=200)
    )

    with patch("crm_dal.check_health", return_value={"status": "error", "error": "connection refused"}):
        r = await test_client.get("/health")
        data = r.json()
        assert data["status"] == "degraded"
        assert "error" in data["services"]["crm"]


@pytest.mark.asyncio
async def test_health_degraded_when_memory_down(test_client, mock_http_client):
    """When Memory service connection fails, health reports degraded."""

    async def route_get(url, **kwargs):
        raise httpx.ConnectError("Connection refused")

    mock_http_client.get = AsyncMock(side_effect=route_get)

    with patch("crm_dal.check_health", return_value={"status": "ok"}):
        r = await test_client.get("/health")
        data = r.json()
        assert data["status"] == "degraded"
        assert "error" in data["services"]["memory"]


# ─── Resolve Contact ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_contact_missing_fields(test_client):
    """Missing channel or identifier returns 400."""
    r = await test_client.post("/resolve-contact", json={"channel": "email"})
    assert r.status_code == 400
    assert "identifier" in r.json()["error"]

    r = await test_client.post("/resolve-contact", json={"identifier": "x@y.com"})
    assert r.status_code == 400
    assert "channel" in r.json()["error"]


@pytest.mark.asyncio
async def test_resolve_contact_existing(test_client):
    """When crm_dal returns a resolved contact, returns it."""
    resolved = {
        "person_id": "abc-123",
        "channel": "email",
        "identifier": "test@test.com",
        "display_name": "Test User",
    }

    with patch("crm_dal.resolve_contact", return_value=resolved):
        r = await test_client.post("/resolve-contact", json={
            "channel": "email",
            "identifier": "test@test.com",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["person_id"] == "abc-123"


# ─── CRM Proxy Endpoints ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_list_conversations(test_client):
    """Proxy list_conversations returns CRM data."""
    mock_result = {"data": {"payload": [{"id": 1}], "meta": {}}}

    with patch("crm_dal.list_conversations", return_value=mock_result):
        r = await test_client.get("/api/conversations?status=open")
        assert r.status_code == 200
        assert r.json() == mock_result


@pytest.mark.asyncio
async def test_api_get_conversation_not_found(test_client):
    """When conversation doesn't exist, returns 404."""
    with patch("crm_dal.get_conversation", return_value=None):
        r = await test_client.get("/api/conversations/9999")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_create_message_empty_content(test_client):
    """Empty content returns 400."""
    r = await test_client.post("/api/conversations/1/messages", json={
        "content": "",
    })
    assert r.status_code == 400
    assert "content" in r.json()["error"]


@pytest.mark.asyncio
async def test_api_create_person_missing_name(test_client):
    """Missing firstName returns 400."""
    r = await test_client.post("/api/people", json={
        "lastName": "Smith",
    })
    assert r.status_code == 400
    assert "firstName" in r.json()["error"]


@pytest.mark.asyncio
async def test_api_create_note_missing_title(test_client):
    """Missing title returns 400."""
    r = await test_client.post("/api/notes", json={
        "body": "some note body",
    })
    assert r.status_code == 400
    assert "title" in r.json()["error"]


# ─── Log Interaction ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_interaction_resolves_and_logs(test_client):
    """log-interaction resolves contact via crm_dal and creates conversation record."""
    resolved = {
        "person_id": "person-1",
        "channel": "api",
        "identifier": "Alice",
        "display_name": "Alice",
    }

    with patch("crm_dal.resolve_contact", return_value=resolved), \
         patch("crm_dal.get_conversations_for_contact", return_value=[{"id": 10}]), \
         patch("crm_dal.send_message", return_value={"id": 100}):

        r = await test_client.post("/log-interaction", json={
            "contact_name": "Alice",
            "channel": "api",
            "direction": "outgoing",
            "content_summary": "Called Alice about project status",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["resolved"] is True
