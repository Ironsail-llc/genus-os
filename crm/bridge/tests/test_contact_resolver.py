"""
Tests for contact_resolver.py.

All database calls are mocked via mock_db fixture.
All HTTP calls (Twenty, Chatwoot, Memory) are mocked.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import contact_resolver  # noqa: E402


# ─── resolve() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_existing_complete_mapping(mock_db, sample_contact_row):
    """Complete mapping (both IDs present) returns immediately, no HTTP calls."""
    mock_db["cursor"].fetchone.return_value = sample_contact_row
    client = AsyncMock(spec=httpx.AsyncClient)

    result = await contact_resolver.resolve("email", sample_contact_row["identifier"], client=client)

    assert result["twenty_person_id"] == "twenty-abc-123"
    assert result["chatwoot_contact_id"] == 42
    # No HTTP calls should have been made
    client.get.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_existing_incomplete_fills_gaps(mock_db):
    """Row has twenty_id but no chatwoot_id — searches Chatwoot and upserts."""
    incomplete_row = {
        "id": 1, "channel": "email", "identifier": "partial@test.com",
        "display_name": "Partial User", "twenty_person_id": "twenty-abc",
        "chatwoot_contact_id": None, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    upserted_row = {**incomplete_row, "chatwoot_contact_id": 77}

    mock_db["cursor"].fetchone.side_effect = [incomplete_row, upserted_row]
    client = AsyncMock(spec=httpx.AsyncClient)

    with patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[{"id": 77}]):
        result = await contact_resolver.resolve("email", "partial@test.com", client=client)

    assert result["twenty_person_id"] == "twenty-abc"
    assert result["chatwoot_contact_id"] == 77


@pytest.mark.asyncio
async def test_resolve_new_contact_by_email(mock_db):
    """No existing row — searches Twenty, creates in Chatwoot, inserts mapping."""
    new_row = {
        "id": 3, "channel": "email", "identifier": "brand-new@test.com",
        "display_name": "Brand New", "twenty_person_id": "twenty-new",
        "chatwoot_contact_id": 88, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchone.side_effect = [None, new_row]
    client = AsyncMock(spec=httpx.AsyncClient)

    with patch("contact_resolver.twenty_client.search_people", new_callable=AsyncMock, return_value=[{"id": "twenty-new"}]), \
         patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[]), \
         patch("contact_resolver.chatwoot_client.create_contact", new_callable=AsyncMock, return_value={"id": 88}):
        result = await contact_resolver.resolve("email", "brand-new@test.com", "Brand New", client)

    assert result["twenty_person_id"] == "twenty-new"
    assert result["chatwoot_contact_id"] == 88


@pytest.mark.asyncio
async def test_resolve_new_contact_by_phone(mock_db):
    """Channel=voice uses phone field for Twenty/Chatwoot creation."""
    new_row = {
        "id": 4, "channel": "voice", "identifier": "+15551234567",
        "display_name": "Phone User", "twenty_person_id": "twenty-phone",
        "chatwoot_contact_id": 99, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchone.side_effect = [None, new_row]
    client = AsyncMock(spec=httpx.AsyncClient)

    with patch("contact_resolver.twenty_client.search_people", new_callable=AsyncMock, return_value=[]) as mock_search, \
         patch("contact_resolver.twenty_client.create_person", new_callable=AsyncMock, return_value="twenty-phone") as mock_create, \
         patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[]), \
         patch("contact_resolver.chatwoot_client.create_contact", new_callable=AsyncMock, return_value={"id": 99}) as mock_cw_create:
        result = await contact_resolver.resolve("voice", "+15551234567", "Phone User", client)

    # Verify phone was passed (not email) to create_person
    mock_create.assert_called_once()
    call_args = mock_create.call_args
    assert call_args[0][2] is None  # email=None
    assert call_args[0][3] == "+15551234567"  # phone=identifier


@pytest.mark.asyncio
async def test_resolve_creates_twenty_person_when_not_found(mock_db):
    """When Twenty search returns empty and name is provided, creates a new person."""
    new_row = {
        "id": 5, "channel": "email", "identifier": "unknown@test.com",
        "display_name": "Unknown User", "twenty_person_id": "twenty-created",
        "chatwoot_contact_id": 55, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchone.side_effect = [None, new_row]
    client = AsyncMock(spec=httpx.AsyncClient)

    with patch("contact_resolver.twenty_client.search_people", new_callable=AsyncMock, return_value=[]), \
         patch("contact_resolver.twenty_client.create_person", new_callable=AsyncMock, return_value="twenty-created") as mock_create, \
         patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[]), \
         patch("contact_resolver.chatwoot_client.create_contact", new_callable=AsyncMock, return_value={"id": 55}):
        result = await contact_resolver.resolve("email", "unknown@test.com", "Unknown User", client)

    mock_create.assert_called_once_with("Unknown", "User", "unknown@test.com", None, client)
    assert result["twenty_person_id"] == "twenty-created"


@pytest.mark.asyncio
async def test_resolve_upsert_preserves_existing_ids(mock_db):
    """COALESCE logic: upsert doesn't null out existing IDs when new values are None."""
    # Row exists with chatwoot_id but no twenty_id
    existing = {
        "id": 6, "channel": "telegram", "identifier": "tg_user",
        "display_name": "TG User", "twenty_person_id": None,
        "chatwoot_contact_id": 33, "memory_entity_id": "ent-1",
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    # After upsert, chatwoot_id and entity_id preserved, twenty_id filled
    upserted = {**existing, "twenty_person_id": "twenty-found"}

    mock_db["cursor"].fetchone.side_effect = [existing, upserted]
    client = AsyncMock(spec=httpx.AsyncClient)

    with patch("contact_resolver.twenty_client.search_people", new_callable=AsyncMock, return_value=[{"id": "twenty-found"}]), \
         patch("contact_resolver.chatwoot_client.search_contacts", new_callable=AsyncMock, return_value=[{"id": 33}]):
        result = await contact_resolver.resolve("telegram", "tg_user", client=client)

    # Verify the upsert SQL was called with the right values
    execute_calls = mock_db["cursor"].execute.call_args_list
    upsert_call = [c for c in execute_calls if "INSERT INTO contact_identifiers" in str(c)]
    assert len(upsert_call) == 1

    # Result should have both IDs
    assert result["twenty_person_id"] == "twenty-found"
    assert result["chatwoot_contact_id"] == 33


# ─── get_timeline() ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_timeline_empty(mock_db):
    """Unknown identifier returns structure with empty arrays."""
    mock_db["cursor"].fetchall.return_value = []
    client = AsyncMock(spec=httpx.AsyncClient)

    result = await contact_resolver.get_timeline("nonexistent", client)

    assert result["identifier"] == "nonexistent"
    assert result["mappings"] == []
    assert result["twenty"] is None
    assert result["chatwoot_conversations"] == []
    assert result["memory_facts"] == []


@pytest.mark.asyncio
async def test_get_timeline_with_data(mock_db):
    """Identifier with mappings returns Twenty person, Chatwoot convos, and memory facts."""
    mapping = {
        "id": 1, "channel": "email", "identifier": "alice@test.com",
        "display_name": "Alice", "twenty_person_id": "twenty-alice",
        "chatwoot_contact_id": 10, "memory_entity_id": None,
        "created_at": "2026-02-13T00:00:00", "updated_at": "2026-02-13T00:00:00",
    }
    mock_db["cursor"].fetchall.return_value = [mapping]

    client = AsyncMock(spec=httpx.AsyncClient)
    memory_response = MagicMock(spec=httpx.Response, status_code=200)
    memory_response.json.return_value = {"results": [{"fact": "Alice likes tea"}]}
    client.get = AsyncMock(return_value=memory_response)

    with patch("contact_resolver.twenty_client.get_person", new_callable=AsyncMock,
               return_value={"id": "twenty-alice", "name": {"firstName": "Alice", "lastName": "Test"}}), \
         patch("contact_resolver.chatwoot_client.get_conversations", new_callable=AsyncMock,
               return_value=[{"id": 100, "status": "open"}]):

        result = await contact_resolver.get_timeline("alice@test.com", client)

    assert result["identifier"] == "alice@test.com"
    assert len(result["mappings"]) == 1
    assert result["twenty"]["id"] == "twenty-alice"
    assert len(result["chatwoot_conversations"]) == 1
    assert len(result["memory_facts"]) == 1
