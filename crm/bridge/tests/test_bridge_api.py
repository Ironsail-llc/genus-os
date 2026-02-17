"""
Tests for bridge_service.py endpoints.

All external HTTP calls (Twenty, Chatwoot, Memory) are mocked.
Database calls in contact_resolver are mocked via mock_db fixture.
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
    """When all 3 services return 200, health reports status: ok."""
    resp_ok = MagicMock(spec=httpx.Response, status_code=200)

    async def route_get(url, **kwargs):
        return resp_ok

    mock_http_client.get = AsyncMock(side_effect=route_get)

    r = await test_client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["services"]["twenty"] == "ok"
    assert data["services"]["chatwoot"] == "ok"
    assert data["services"]["memory"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded_when_twenty_down(test_client, mock_http_client):
    """When Twenty returns 500, health reports degraded."""
    resp_ok = MagicMock(spec=httpx.Response, status_code=200)
    resp_err = MagicMock(spec=httpx.Response, status_code=500)

    async def route_get(url, **kwargs):
        if "3030" in url or "healthz" in url:
            return resp_err
        return resp_ok

    mock_http_client.get = AsyncMock(side_effect=route_get)

    r = await test_client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "degraded"
    assert "error" in data["services"]["twenty"]


@pytest.mark.asyncio
async def test_health_degraded_when_chatwoot_down(test_client, mock_http_client):
    """When Chatwoot connection fails, health reports degraded."""

    async def route_get(url, **kwargs):
        if "3100" in url or "/api" in url:
            raise httpx.ConnectError("Connection refused")
        return MagicMock(spec=httpx.Response, status_code=200)

    mock_http_client.get = AsyncMock(side_effect=route_get)

    r = await test_client.get("/health")
    data = r.json()
    assert data["status"] == "degraded"
    assert "error" in data["services"]["chatwoot"]


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
async def test_resolve_contact_existing_mapping(test_client, mock_db, sample_contact_row):
    """When DB has a complete mapping, returns it directly without HTTP calls."""
    mock_db["cursor"].fetchone.return_value = sample_contact_row

    r = await test_client.post("/resolve-contact", json={
        "channel": "email",
        "identifier": sample_contact_row["identifier"],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["twenty_person_id"] == "twenty-abc-123"
    assert data["chatwoot_contact_id"] == 42


@pytest.mark.asyncio
async def test_resolve_contact_creates_new(test_client, mock_db):
    """When no mapping exists, searches and creates in external systems."""
    # First fetchone returns None (no existing mapping)
    # After upsert, fetchone returns the new row
    new_row = {
        "id": 2, "channel": "email", "identifier": "new@test.com",
        "display_name": "New User", "twenty_person_id": "twenty-new-1",
        "chatwoot_contact_id": 99, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchone.side_effect = [None, new_row]

    with patch("contact_resolver.twenty_client.search_people", new_callable=AsyncMock, return_value=[]) as mock_search, \
         patch("contact_resolver.twenty_client.create_person", new_callable=AsyncMock, return_value="twenty-new-1"), \
         patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[]), \
         patch("contact_resolver.chatwoot_client.create_contact", new_callable=AsyncMock, return_value={"id": 99}):

        r = await test_client.post("/resolve-contact", json={
            "channel": "email",
            "identifier": "new@test.com",
            "name": "New User",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["twenty_person_id"] == "twenty-new-1"
        assert data["chatwoot_contact_id"] == 99


# ─── Webhooks ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chatwoot_webhook_message_created(test_client, mock_http_client):
    """message_created event ingests to memory system."""
    mock_http_client.post = AsyncMock(
        return_value=MagicMock(spec=httpx.Response, status_code=200)
    )

    r = await test_client.post("/webhooks/chatwoot", json={
        "event": "message_created",
        "content": {"content": "Hello from customer"},
        "sender": {"name": "Test User", "email": "test@example.com"},
        "conversation": {"id": 42},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Verify it tried to POST to memory /ingest
    mock_http_client.post.assert_called_once()
    call_args = mock_http_client.post.call_args
    assert "/ingest" in call_args[0][0]


@pytest.mark.asyncio
async def test_chatwoot_webhook_unknown_event(test_client, mock_http_client):
    """Unknown event type still returns 200 (don't fail webhook delivery)."""
    r = await test_client.post("/webhooks/chatwoot", json={
        "event": "conversation_updated",
        "conversation": {"id": 10},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_twenty_webhook_passthrough(test_client):
    """Twenty webhook accepts any payload and returns ok with event echo."""
    r = await test_client.post("/webhooks/twenty", json={
        "event": "person.created",
        "data": {"id": "abc"},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["event"] == "person.created"


# ─── CRM Proxy Endpoints ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_list_conversations(test_client):
    """Proxy list_conversations returns Chatwoot data."""
    mock_result = {"data": {"payload": [{"id": 1}], "meta": {}}}

    with patch("chatwoot_client.list_conversations", new_callable=AsyncMock, return_value=mock_result):
        r = await test_client.get("/api/conversations?status=open")
        assert r.status_code == 200
        assert r.json() == mock_result


@pytest.mark.asyncio
async def test_api_get_conversation_not_found(test_client):
    """When conversation doesn't exist, returns 404."""
    with patch("chatwoot_client.get_conversation", new_callable=AsyncMock, return_value=None):
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
async def test_log_interaction_resolves_and_logs(test_client, mock_db):
    """log-interaction resolves contact and creates Chatwoot record."""
    resolved_row = {
        "id": 1, "channel": "api", "identifier": "Alice",
        "display_name": "Alice", "twenty_person_id": "twenty-1",
        "chatwoot_contact_id": 50, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchone.return_value = resolved_row

    with patch("chatwoot_client.get_conversations", new_callable=AsyncMock, return_value=[{"id": 10}]), \
         patch("chatwoot_client.send_message", new_callable=AsyncMock, return_value={"id": 100}):

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
