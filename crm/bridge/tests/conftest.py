"""
Shared test fixtures for the Bridge Service test suite.

Provides async test client, database connections, test data isolation,
and mock helpers for external HTTP calls.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Add bridge source directory to path so imports resolve
BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))

import bridge_service  # noqa: E402
from bridge_service import app  # noqa: E402


@pytest.fixture
def test_prefix():
    """Unique prefix to tag all test data for isolation and cleanup."""
    return f"__test_{uuid.uuid4().hex[:8]}__"


@pytest_asyncio.fixture
async def test_client():
    """Async HTTP client wrapping the Bridge FastAPI app via ASGITransport."""
    # Create a mock http_client for the bridge service lifespan
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    bridge_service.http_client = mock_http

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    bridge_service.http_client = None


@pytest.fixture
def mock_http_client():
    """Direct access to the mocked httpx.AsyncClient used by bridge_service."""
    mock = AsyncMock(spec=httpx.AsyncClient)
    bridge_service.http_client = mock
    yield mock
    bridge_service.http_client = None


def _make_response(status_code=200, json_data=None):
    """Helper to create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


@pytest.fixture
def mock_services_healthy(mock_http_client):
    """Configure mock_http_client so all health checks return 200."""

    async def route_get(url, **kwargs):
        return _make_response(200, {})

    mock_http_client.get = AsyncMock(side_effect=route_get)
    return mock_http_client


@pytest.fixture
def mock_db(test_prefix):
    """Mock the database connection used by contact_resolver."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []

    with patch("contact_resolver.get_db", return_value=mock_conn):
        yield {
            "conn": mock_conn,
            "cursor": mock_cursor,
            "test_prefix": test_prefix,
        }


@pytest.fixture
def sample_contact_row(test_prefix):
    """A pre-built contact_identifiers row for testing."""
    return {
        "id": 1,
        "channel": "email",
        "identifier": f"{test_prefix}@test.com",
        "display_name": f"{test_prefix} User",
        "twenty_person_id": "twenty-abc-123",
        "chatwoot_contact_id": 42,
        "memory_entity_id": "entity-xyz-789",
        "created_at": "2026-02-11T00:00:00",
        "updated_at": "2026-02-11T00:00:00",
    }
