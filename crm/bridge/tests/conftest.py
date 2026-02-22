"""
Shared test fixtures for the Bridge Service test suite.

Provides async test client, mock helpers for crm_dal and external HTTP calls.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Add bridge source directory and memory system to path so imports resolve
BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, "/home/philip/clawd/memory_system")

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
