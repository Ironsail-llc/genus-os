"""
Shared test fixtures for the Bridge Service test suite.

Provides async test client, mock helpers for crm_dal and external HTTP calls.
"""

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Bridge tests can run from their own rootdir (`pytest crm/bridge/tests/`),
# where the repo-root conftest.py — and its ROBOTHOR_DB_NAME pin — is not
# loaded. Duplicate the pin here, before any robothor import resolves config,
# so bridge tests can never write to the production database either.
# setdefault keeps an explicit CI/dev ROBOTHOR_DB_NAME authoritative.
os.environ.setdefault("ROBOTHOR_DB_NAME", "robothor_test")

# Same reasoning for the event bus: the platform default is production Redis
# (db 0), and every bridge router publishes crm.* / agent.* events that the
# live engine consumes as genuine hooks. robothor/events/bus.py hard-fails
# rather than publishing off-allowlist under pytest; this pin means the
# well-behaved case never has to rely on that backstop.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

# Add bridge source directory to path so imports resolve
BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))

# And the repo root, so the containment fixture below imports whichever rootdir
# this suite is invoked from. NOT wrapped in a suppress: the repo-root
# conftest.py can afford to skip its optional integration fixtures when `tests`
# is unimportable, and this cannot — a containment guard that silently does not
# load is the failure it exists to prevent.
REPO_ROOT = BRIDGE_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import bridge_service  # noqa: E402
from bridge_service import app  # noqa: E402

from robothor.events.capabilities import load_capabilities, reset  # noqa: E402

# Structural containment: no bridge test may reach a real workspace.
#
# The routers this suite exercises resolve their write paths from
# `EngineConfig.from_env().workspace`, which falls back to `~/robothor` when
# ROBOTHOR_WORKSPACE is unset — on a developer's box, the live fleet. The agent
# builder writes `docs/agents/<id>.yaml` and `brain/<ID>.md`, which is the exact
# file class the 2026-09-12 incident destroyed, and a per-test `workspace`
# fixture protects only the tests that remember it.
#
# `contained_workspace` is autouse: it pins HOME/ROBOTHOR_WORKSPACE to a
# throwaway directory AND checks a sentinel manifest byte-for-byte afterwards,
# so the redirect is verified rather than assumed. Same wiring as
# robothor/cli/tests/conftest.py and robothor/init/tests/conftest.py; one copy
# of the fixture, because two copies of a guard drift invisibly.
from tests.conftest_workspace_containment import (  # noqa: E402, F401 - pytest collects these
    assert_contained,
    contained_workspace,
    env_workspace,
)

# Platform-owned test fixture — NOT the operator's live brain/agent_capabilities.json.
# That file is instance-owned (CLAUDE.md rule 11) and gitignored, so it doesn't exist
# on a fresh clone/CI checkout; the bridge RBAC suite needs its own tracked manifest
# with the same shape so `test_rbac.py`'s allow/deny assertions hold everywhere.
CAPABILITIES_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "agent_capabilities.json"


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


#: Where a bridge test's engine call is allowed to go: nowhere.
#:
#: ``.invalid`` is reserved by RFC 2606 and guaranteed never to resolve, so even
#: if every layer below were removed the call would fail DNS rather than reach
#: somebody's engine.
UNROUTABLE_HOST = "engine.invalid"
UNROUTABLE_ENGINE = f"http://{UNROUTABLE_HOST}:1"

#: The only two httpx transports that open a socket. Everything else — the ASGI
#: one the test clients mount the app on, the mock one a test installs to stand
#: in for a server, starlette's own TestClient transport, a WSGI one — stays in
#: the process by construction.
#:
#: Enumerated this way round on purpose. Listing the SAFE transports meant a
#: client using one nobody had thought of was reported as an escape, and
#: starlette's TestClient (which does not use httpx.ASGITransport) was exactly
#: that. Listing the two dangerous ones fails closed instead: a new in-process
#: transport just works, and a new way to reach the network does not.
#:
#: Asking about the TRANSPORT rather than the host is what makes this airtight:
#: 127.0.0.1 on a real transport is the operator's own engine on :18800, which
#: is precisely the call that must not happen.
_NETWORK_TRANSPORTS = (httpx.HTTPTransport, httpx.AsyncHTTPTransport)


@pytest.fixture(autouse=True)
def no_engine_calls_from_the_bridge_suite(monkeypatch):
    """No bridge test may reach a real engine.

    Several routers call the engine — the agent builder, the marketplace
    installer, the first-run wizard, the provider wizard — and ``engine_request``
    is a plain HTTP POST to whatever ``ROBOTHOR_ENGINE_URL`` resolves to. On a
    developer's box that is their LIVE engine, so an unpatched test would
    rebuild the operator's job set, or reload their provider keys, from a unit
    suite.

    Patched at the SINK, not at the callers. The first version of this fixture
    named three attributes (``routers.agent_manifests.engine_request`` and two
    ``reconcile_engine_schedules``), and every router imports those by value —
    so ``routers.providers.engine_request`` and ``setup.py``'s two
    function-local imports were untouched, and a router added tomorrow would be
    unguarded by construction. That is precisely the failure mode this
    docstring claims to be replacing, so it is worth saying twice: a guard
    bound to the names that exist today is a guard that stops covering the code
    written tomorrow.

    ``engine_base_url`` is the one funnel every caller goes through, and the
    ``httpx`` transports under it are a second lock for anything that builds a
    URL another way. Per-test ``FakeEngine`` patches still win, because they
    replace ``engine_request`` above both.

    **What this does and does not cover.** Four httpx entry points are patched:
    ``AsyncClient.request``/``send`` and the SYNC ``Client.request``/``send``.
    The sync ones are not hypothetical — ``templates/hub_client.py`` uses
    ``httpx.Client``, and ``POST /api/installed-agents/install`` reaches it. Not
    covered: ``urllib.request.urlopen`` (``routers/setup.py`` uses it for the
    Ollama probe) and anything using ``socket`` directly. So the honest claim is
    "nothing in this suite reaches a real host over httpx, and the ENGINE seam
    specifically is closed at its funnel" — not "a unit suite cannot dial
    anything", which is what the assertion used to say and could not deliver.
    """
    monkeypatch.setenv("ROBOTHOR_ENGINE_URL", UNROUTABLE_ENGINE)

    def _verdict(client, url):
        """``None`` to allow, else the exception to raise."""
        if not isinstance(getattr(client, "_transport", None), _NETWORK_TRANSPORTS):
            return None
        # A relative URL has no host of its own; it resolves against the
        # client's base_url. Reading only the argument would report every such
        # request as an escape to ''.
        host = httpx.URL(url).host or client.base_url.host
        if host == UNROUTABLE_HOST:
            # Where the env pin above sends an unpatched engine call. Refused
            # here rather than left to DNS: instant, and independent of what
            # this box's resolver decides to do with an unknown name. The route
            # sees the same ConnectError it would from a dead engine, so the
            # "engine unreachable" branch stays exercised rather than mocked
            # out of existence.
            return httpx.ConnectError(f"refused by the bridge test suite: {host}")
        return AssertionError(
            f"a bridge test tried to reach {host!r} over a real httpx transport. "
            "Patch the seam your route uses, or mount a fake — 127.0.0.1 is this "
            "developer's own engine."
        )

    real_async_request = httpx.AsyncClient.request
    real_async_send = httpx.AsyncClient.send
    real_sync_request = httpx.Client.request
    real_sync_send = httpx.Client.send

    async def _async_request(self, method, url, *args, **kwargs):
        refusal = _verdict(self, url)
        if refusal is not None:
            raise refusal
        return await real_async_request(self, method, url, *args, **kwargs)

    async def _async_send(self, request, *args, **kwargs):
        refusal = _verdict(self, request.url)
        if refusal is not None:
            raise refusal
        return await real_async_send(self, request, *args, **kwargs)

    def _sync_request(self, method, url, *args, **kwargs):
        refusal = _verdict(self, url)
        if refusal is not None:
            raise refusal
        return real_sync_request(self, method, url, *args, **kwargs)

    def _sync_send(self, request, *args, **kwargs):
        refusal = _verdict(self, request.url)
        if refusal is not None:
            raise refusal
        return real_sync_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", _async_request)
    monkeypatch.setattr(httpx.AsyncClient, "send", _async_send)
    monkeypatch.setattr(httpx.Client, "request", _sync_request)
    monkeypatch.setattr(httpx.Client, "send", _sync_send)


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
def controls_client_as_auditor(_controls_auth_key):
    """A verified human session with role="auditor" — the one non-operator
    human role the audit READ surfaces admit.

    ``robothor.auth.tokens`` grants it ``audit:read`` and nothing else that
    writes, and ``ROLE_DESCRIPTIONS`` calls it "Read-only, plus the audit log.
    For review, not operation." Every route that CHANGES something must still
    403 it, which is what the memory/logs suites assert with this fixture.
    """
    return _make_controls_client_as_role("auditor")


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


@pytest.fixture(autouse=True)
def _fresh_settings_cache():
    """The settings singleton is cached per process; a test that sets an env var
    must see it, so the cache is cleared before and after every bridge test (the
    root conftest does the same for the rest of the tree)."""
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


# ── the engine's own flag readers ────────────────────────────────────────────
#
# Lives here rather than in one test file because TWO suites now need it, and
# they need it for the same reason: the only honest check of what a page shows
# for a guardrail is what the ENGINE resolves for it. Two pages agreeing with
# each other proved nothing -- ``normalise`` was wrong and both pages were
# wrong together, which is precisely how the 2026-09-15 re-review found a
# guardrail reported ``false`` while the engine ran it.
#
# Enumerated, not derived, so adding a governed flag stops HERE and is
# considered rather than silently dropping out of the comparison; the guard
# test in ``test_controls_unset_defaults.py`` fails when the table and
# ``GOVERNED_FLAGS`` disagree.


def engine_flag_readers() -> dict[str, tuple[object, str]]:
    """flag -> (the engine accessor, the env var that gates it, or "").

    The gate matters: a two-var ladder's accessor returns ``off`` while its
    subsystem-enabled var is unset, which is a fact about that OTHER flag, not
    about the value of the one a page is rendering. Setting the gate isolates
    the question to "what does this flag itself resolve to".
    """
    from robothor.engine import feature_flags as ff

    return {
        "ROBOTHOR_ADMISSION_MODE": (ff.execution_mode_admission_mode, "ROBOTHOR_ADMISSION_ENABLED"),
        "ROBOTHOR_APPROVAL_MODE": (ff.approval_mode, "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED"),
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE": (
            ff.benchmark_decontamination_mode,
            "ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED",
        ),
        "ROBOTHOR_BENCHMARK_SANDBOX_MODE": (
            ff.benchmark_sandbox_mode,
            "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED",
        ),
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE": (
            ff.completion_contract_mode,
            "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED",
        ),
        "ROBOTHOR_DELIVERABLE_CONTRACT_MODE": (
            ff.deliverable_contract_mode,
            "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED",
        ),
        "ROBOTHOR_DNC_MODE": (ff.do_not_contact_mode, ""),
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE": (
            ff.exec_allowlist_mode,
            "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED",
        ),
        "ROBOTHOR_HONESTY_SUITE_MODE": (ff.honesty_suite_mode, ""),
        "ROBOTHOR_INJECTION_SCAN_MODE": (ff.injection_scan_mode, "ROBOTHOR_INJECTION_SCAN_ENABLED"),
        "ROBOTHOR_JUDGE_ENABLED": (ff.goal_judge_enabled, ""),
        "ROBOTHOR_PER_USER_SESSIONS": (ff.per_user_sessions_mode, ""),
        "ROBOTHOR_RBAC_MODE": (ff.rbac_enforcement_mode, "ROBOTHOR_RBAC_ENABLED"),
        "ROBOTHOR_RIP_13_MODE": (ff.symbolic_memory_mode, "ROBOTHOR_RIP_13_ENABLED"),
        "ROBOTHOR_RIP_1_ENABLED": (lambda: ff.is_rip_enabled(1), ""),
        "ROBOTHOR_RIP_4_ENABLED": (lambda: ff.is_rip_enabled(4), ""),
        "ROBOTHOR_RIP_5_ENABLED": (ff.curator_enabled, ""),
        "ROBOTHOR_RIP_7_MODE": (ff.rip_7_enforcement_mode, "ROBOTHOR_RIP_7_ENABLED"),
        "ROBOTHOR_RUN_VERIFICATION_MODE": (
            ff.run_verification_mode,
            "ROBOTHOR_RUN_VERIFICATION_ENABLED",
        ),
        "ROBOTHOR_SANDBOX_DEFAULT_MODE": (
            ff.sandbox_default_mode,
            "ROBOTHOR_SANDBOX_DEFAULT_ENABLED",
        ),
        "ROBOTHOR_STEP_EFFICIENCY_MODE": (ff.step_efficiency_mode, ""),
        "ROBOTHOR_TOOL_VERIFY_MODE": (ff.tool_verify_mode, "ROBOTHOR_TOOL_VERIFY_ENABLED"),
    }


def engine_flag_value(value: object) -> str:
    """An engine accessor's answer, spelled the way the store and the API spell it."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


@pytest.fixture
def engine_readers():
    """:func:`engine_flag_readers`, for a test that wants it as a fixture."""
    return engine_flag_readers()
