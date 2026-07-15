"""
Shared test fixtures for the Bridge Service test suite.

Provides async test client, mock helpers for crm_dal and external HTTP calls.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Add bridge source directory to path so imports resolve
BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))

import bridge_service  # noqa: E402
from bridge_service import app  # noqa: E402

from robothor.events.capabilities import load_capabilities, reset  # noqa: E402

CAPABILITIES_MANIFEST = BRIDGE_DIR.parents[1] / "brain" / "agent_capabilities.json"


@pytest.fixture(autouse=True)
def insecure_loopback_dev_mode(monkeypatch):
    """Legacy bridge tests opt into the only supported trusted-header mode."""
    monkeypatch.setenv("GENUS_INSECURE_DEV_MODE", "true")
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("ROBOTHOR_CAPABILITIES_MANIFEST", str(CAPABILITIES_MANIFEST))
    monkeypatch.delenv("GENUS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ROBOTHOR_ENVIRONMENT", raising=False)
    reset()
    load_capabilities()
    yield
    reset()


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
def _controls_auth_key(monkeypatch):
    """A stable signing key for tests that mint real tokens via robothor.auth.tokens."""
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.fixture
def controls_client_as_operator(_controls_auth_key):
    """A verified human operator session (typ="user", role="owner") in the
    PLATFORM tenant.

    ``AuthContext.is_service`` is False, so the controls router's
    ``_require_operator`` check lets it through. ``feature_flags`` is a
    single GLOBAL table, so the tenant must also match
    ``routers.controls.PLATFORM_TENANT`` — see
    ``controls_client_as_other_tenant_owner`` for the tenant that must be
    rejected despite carrying the same role.

    Deliberately NOT used as a context manager: entering/exiting
    ``TestClient(app)`` runs the real ASGI lifespan (spins up the routine
    trigger background task + a live ``httpx.AsyncClient``), which the
    controls routes never touch and which was observed to perturb the tight
    thread-scheduling timing in ``test_route_concurrency.py`` when run in the
    same session. The bare client still serves requests correctly (see
    ``bridge_service.http_client`` staying unused by these routes).
    """
    from fastapi.testclient import TestClient
    from routers.controls import PLATFORM_TENANT

    from robothor.auth import tokens

    token = tokens.issue_access_token("operator-1", PLATFORM_TENANT, "owner")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _make_controls_client_as_role(role: str, tenant_id: str | None = None):
    """Mint a verified human session (typ="user") with a given role and wrap
    it in a bare ``TestClient`` — see ``controls_client_as_operator`` for why
    the client is not used as a context manager. Defaults to the PLATFORM
    tenant so role is the only variable under test unless a caller overrides
    ``tenant_id`` (e.g. to exercise the cross-tenant reject path)."""
    from fastapi.testclient import TestClient
    from routers.controls import PLATFORM_TENANT

    from robothor.auth import tokens

    token = tokens.issue_access_token("human-1", tenant_id or PLATFORM_TENANT, role)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def controls_client_as_viewer(_controls_auth_key):
    """A verified human session with role="viewer" — a non-operator human
    role that dashboard SSO admits, but which must not reach the controls
    API (only ``owner``/``admin`` may)."""
    return _make_controls_client_as_role("viewer")


@pytest.fixture
def controls_client_as_user(_controls_auth_key):
    """A verified human session with role="user" — same non-operator
    reasoning as ``controls_client_as_viewer``."""
    return _make_controls_client_as_role("user")


@pytest.fixture
def controls_client_as_admin(_controls_auth_key):
    """A verified human session with role="admin" — the second operator
    role alongside "owner"."""
    return _make_controls_client_as_role("admin")


@pytest.fixture
def controls_client_as_other_tenant_owner(_controls_auth_key):
    """A verified human session with role="owner" — the operator role — but
    in a DIFFERENT tenant ("acme-corp") than the platform tenant.

    ``feature_flags`` is a single GLOBAL platform table, not tenant-scoped,
    so an owner/admin role alone must not be sufficient: only the platform
    tenant's operator may read or write it. This fixture proves the tenant
    gate in ``_require_operator`` rejects an otherwise-valid operator role
    from any other tenant.
    """
    return _make_controls_client_as_role("owner", tenant_id="acme-corp")


@pytest.fixture
def controls_client_as_service(_controls_auth_key, monkeypatch):
    """A verified service (agent) session (typ="service") — what every agent
    tool-call carries. ``AuthContext.is_service`` is True.

    The capability manifest's endpoint whitelist is patched permissive here
    on purpose: no agent's ``bridge_endpoints`` entry names ``/api/controls``
    today, so leaving the manifest as-is would make ``RBACMiddleware`` reject
    the request before it ever reaches the router — a green test for the
    wrong reason. Patching it open proves the 403 in
    ``test_patch_rejects_a_service_token`` comes from the router's own
    ``auth.is_service`` check (lock #3), not an incidental manifest gap.

    Bare (non-context-managed) ``TestClient`` for the same reason as
    ``controls_client_as_operator`` above.
    """
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    token = tokens.issue_service_token("email-classifier", "tenant-a", agent_id="email-classifier")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def fake_store(monkeypatch):
    """Records writes so an audited PATCH can be asserted without touching
    the real DB-backed flag store."""
    from robothor.flags.store import GOVERNED_FLAGS
    from robothor.flags.store import valid_values_for as _valid_values_for

    class FakeStore:
        def __init__(self):
            self.GOVERNED_FLAGS = GOVERNED_FLAGS
            self.last_write = None
            self.last_actor = None
            self.last_reason = None
            self.values: dict = {}

        def resolve(self, name):
            return self.values.get(name)

        def valid_values_for(self, name):
            # Delegate to the real (single-source-of-truth) implementation —
            # this fixture only fakes DB-backed persistence, not flag typing.
            return _valid_values_for(name)

        def set_flag(self, name, value, actor, reason):
            self.values[name] = value
            self.last_write = (name, value)
            self.last_actor = actor
            self.last_reason = reason

    fake = FakeStore()
    monkeypatch.setattr("routers.controls.store", fake)
    return fake


@pytest.fixture
def fake_verdict(monkeypatch):
    """Patches the ``verdict`` symbol as imported into ``routers.controls``
    (``from robothor.flags.evidence import verdict``) so GET-path tests never
    open a real DB connection — CI's unit lane runs this test module with no
    database reachable at all.

    Returns canned, deterministic ``Verdict`` objects keyed only by the flag
    name/mode passed in, so callers asserting on payload *shape* (all 12
    flags present, ``verdict.status`` present, ``valid_values`` present) get
    a real router response without touching ``agent_guardrail_events`` or
    any other evidence table.
    """
    from robothor.flags.evidence import Verdict

    def _fake_verdict(name: str, mode: str) -> Verdict:
        return Verdict(
            name=name,
            mode=mode,
            status="UNPROVEN",
            last_fired=None,
            count_7d=0,
            message="test-canned verdict — no DB in this lane",
        )

    monkeypatch.setattr("routers.controls.verdict", _fake_verdict)
    return _fake_verdict


@pytest.fixture
def mock_services_healthy(mock_http_client):
    """Configure mock_http_client so all health checks return 200."""

    async def route_get(url, **kwargs):
        return _make_response(200, {})

    mock_http_client.get = AsyncMock(side_effect=route_get)
    return mock_http_client
