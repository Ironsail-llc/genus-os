"""The orchestrator's memory endpoints act as the caller's tenant.

``/ingest``, ``/stats``, ``/query`` and ``/v1/chat/completions`` used to pass no
tenant, so every caller read and wrote the default tenant's memory. The tenant
now comes from a verified bearer token -- the dashboard forwards the session's
bridge token, and services send a minted service token. A caller with no token
gets only what the single-tenant rule gives it: the default tenant on a
deployment with one active tenant, a refusal on any other.

A bare ``X-Tenant-Id`` never selects a tenant. On main an unauthenticated caller
could reach only the default tenant; honouring the header would let the same
caller read and write any tenant just by naming it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from robothor.api.orchestrator import app
from robothor.constants import DEFAULT_TENANT

TENANT = "tenant-a"
KEY = "test-signing-key-at-least-32-bytes-long-xyz"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.fixture(autouse=True)
def _accounts_stand(request, monkeypatch):
    """Human tokens here name synthetic accounts with no row: let them stand.

    The orchestrator refuses a human token whose account no longer stands
    (``robothor.auth.session_standing``), as the bridge does. The bridge suite
    stubs the same lookup the same way; the tests of the refusal itself opt out.
    """
    if "real_session_standing" in request.fixturenames:
        return
    from robothor.auth import session_standing

    monkeypatch.setattr(session_standing, "stands", lambda user_id, home_tenant_id: True)


@pytest.fixture
def real_session_standing():
    """Marker: run the real account-standing lookup (see ``_accounts_stand``)."""


def _tenants(monkeypatch, ids):
    import robothor.crm.dal as dal

    monkeypatch.setattr(
        dal, "list_tenants", lambda parent_id=None, active_only=True: [{"id": i} for i in ids]
    )


def _multi(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _bearer(tenant):
    from robothor.auth.tokens import issue_access_token

    return {"Authorization": f"Bearer {issue_access_token('alice', tenant, 'admin')}"}


def _service_bearer(tenant):
    from robothor.auth.tokens import issue_service_token

    return {"Authorization": f"Bearer {issue_service_token('vision', tenant)}"}


def _ingest():
    return patch(
        "robothor.memory.ingestion.ingest_content",
        new=AsyncMock(return_value={"facts_processed": 0}),
    )


BODY = {"content": "Alice prefers tea", "source_channel": "api"}


# ─── A verified token decides ───────────────────────────────────────


async def test_ingest_stores_under_the_verified_token_tenant(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == TENANT


async def test_a_service_token_acts_as_its_tenant(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=_service_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == TENANT


async def test_a_header_restating_the_token_tenant_is_accepted(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post(
                "/ingest", json=BODY, headers={**_bearer(TENANT), "X-Tenant-Id": TENANT}
            )
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == TENANT


async def test_a_header_contradicting_the_token_is_refused(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post(
                "/ingest", json=BODY, headers={**_bearer(TENANT), "X-Tenant-Id": "tenant-b"}
            )
    assert r.status_code == 403
    ingest.assert_not_called()


def _forged_jwt(tenant):
    import jwt

    return jwt.encode(
        {"sub": "mallory", "tid": tenant, "role": "admin", "aud": "genus-bridge"},
        "some-other-key-that-is-at-least-32-bytes-long",
        algorithm="HS256",
    )


async def test_an_invalid_token_is_refused(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post(
                "/ingest", json=BODY, headers={"Authorization": f"Bearer {_forged_jwt(TENANT)}"}
            )
    assert r.status_code == 401
    ingest.assert_not_called()


# ─── The request runs bound to the token's tenant (RLS) ─────────────


def _scope_recorder(return_value):
    """A stand-in that records the tenant its connections would be bound to."""
    from robothor.db.connection import current_tenant_scope

    seen: list[str | None] = []

    def record(*args, **kwargs):
        seen.append(current_tenant_scope())
        return return_value

    return seen, record


async def test_ingest_runs_bound_to_the_token_tenant(monkeypatch):
    _multi(monkeypatch)
    seen, record = _scope_recorder({"facts_processed": 0})

    async def ingest(**kwargs):
        return record(**kwargs)

    with patch("robothor.memory.ingestion.ingest_content", new=ingest):
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert seen == [TENANT]


async def test_stats_run_bound_to_the_token_tenant(monkeypatch):
    _multi(monkeypatch)
    seen, record = _scope_recorder({"total": 0})
    with patch("robothor.memory.facts.get_memory_stats", new=record):
        async with _client() as c:
            r = await c.get("/stats", headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert seen == [TENANT]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/query", {"question": "what does Alice drink"}),
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "Alice?"}]}),
    ],
)
async def test_query_retrieval_runs_bound_to_the_token_tenant(monkeypatch, path, body):
    _multi(monkeypatch)
    seen, record = _scope_recorder([])
    with (
        patch("robothor.memory.facts.search_facts_compat", new=record),
        patch("robothor.rag.pipeline.search_web", new=AsyncMock(return_value=[])),
        patch("robothor.llm.ollama.generate", new=AsyncMock(return_value="ok")),
        patch("robothor.llm.ollama.chat", new=AsyncMock(return_value="ok")),
    ):
        async with _client() as c:
            r = await c.post(path, json=body, headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert seen == [TENANT]


# ─── The token's scopes and standing, as on the bridge ──────────────


async def test_a_read_only_token_cannot_ingest(monkeypatch):
    from robothor.auth.tokens import issue_access_token

    _multi(monkeypatch)
    viewer = {"Authorization": f"Bearer {issue_access_token('bob', TENANT, 'viewer')}"}
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=viewer)
    assert r.status_code == 403
    ingest.assert_not_called()


async def test_a_read_only_token_can_still_read(monkeypatch):
    from robothor.auth.tokens import issue_access_token

    _multi(monkeypatch)
    viewer = {"Authorization": f"Bearer {issue_access_token('bob', TENANT, 'viewer')}"}
    with patch("robothor.memory.facts.get_memory_stats", return_value={"total": 0}) as stats:
        async with _client() as c:
            r = await c.get("/stats", headers=viewer)
    assert r.status_code == 200, r.text
    assert stats.call_args.kwargs["tenant_id"] == TENANT


async def test_a_write_only_service_token_cannot_read(monkeypatch):
    from robothor.auth.tokens import issue_service_token

    _multi(monkeypatch)
    token = issue_service_token("vision", TENANT, scopes=("bridge:write",))
    with patch("robothor.memory.facts.get_memory_stats") as stats:
        async with _client() as c:
            r = await c.get("/stats", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    stats.assert_not_called()


async def test_a_token_whose_account_no_longer_stands_is_refused(
    monkeypatch, real_session_standing
):
    from robothor.auth import session_standing

    _multi(monkeypatch)
    monkeypatch.setattr(session_standing, "stands", lambda user_id, home_tenant_id: False)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=_bearer(TENANT))
    assert r.status_code == 401
    ingest.assert_not_called()


async def test_an_acting_token_under_enforce_acts_as_the_child_tenant(monkeypatch):
    from robothor.auth.tokens import issue_access_token

    _multi(monkeypatch)
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    acting = issue_access_token("alice", "tenant-b", "admin", home_tenant_id=TENANT)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"Authorization": f"Bearer {acting}"})
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == "tenant-b"


async def test_an_acting_token_is_refused_once_switching_is_off(monkeypatch):
    from robothor.auth.tokens import issue_access_token

    _multi(monkeypatch)
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "off")
    acting = issue_access_token("alice", "tenant-b", "admin", home_tenant_id=TENANT)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"Authorization": f"Bearer {acting}"})
    assert r.status_code == 403
    ingest.assert_not_called()


# ─── An OpenAI client's placeholder key is not a token ──────────────


async def test_a_placeholder_api_key_on_a_single_tenant_deployment_works(monkeypatch):
    """OpenAI-compatible clients always send ``Authorization: Bearer <api key>``.

    A key that is not a JWT is no credential at all, so the request is
    tokenless and gets exactly what a tokenless request gets.
    """
    _tenants(monkeypatch, [DEFAULT_TENANT])
    r, search = await _post_with_stubs(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "hello"}]},
        {"Authorization": "Bearer sk-placeholder"},
    )
    assert r.status_code == 200, r.text
    assert search.call_args.kwargs["tenant_id"] == DEFAULT_TENANT


async def test_a_placeholder_api_key_selects_nothing_on_a_multi_tenant_deployment(monkeypatch):
    _multi(monkeypatch)
    r, search = await _post_with_stubs(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "hello"}]},
        {"Authorization": "Bearer sk-placeholder", "X-Tenant-Id": TENANT},
    )
    assert r.status_code == 401
    search.assert_not_called()


# ─── No token: the single-tenant rule, and a header never selects ───


async def test_a_tokenless_header_cannot_select_a_tenant(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()
    ingest.assert_not_called()


async def test_a_tokenless_header_cannot_leave_a_single_tenant_deployments_tenant(monkeypatch):
    _tenants(monkeypatch, [DEFAULT_TENANT])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"X-Tenant-Id": "tenant-b"})
    assert r.status_code == 401
    ingest.assert_not_called()


async def test_a_tokenless_header_naming_the_only_tenant_is_accepted(monkeypatch):
    _tenants(monkeypatch, [DEFAULT_TENANT])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"X-Tenant-Id": DEFAULT_TENANT})
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == DEFAULT_TENANT


async def test_no_tenant_in_a_multi_tenant_deployment_is_refused(monkeypatch):
    _multi(monkeypatch)
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY)
    assert r.status_code == 400
    assert "tenant" in r.json()["detail"].lower()
    ingest.assert_not_called()


async def test_no_tenant_in_a_single_tenant_deployment_uses_its_tenant(monkeypatch):
    _tenants(monkeypatch, [DEFAULT_TENANT])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY)
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == DEFAULT_TENANT


# ─── /stats ─────────────────────────────────────────────────────────


async def test_stats_count_the_token_tenant(monkeypatch):
    _multi(monkeypatch)
    with patch("robothor.memory.facts.get_memory_stats", return_value={"total": 0}) as stats:
        async with _client() as c:
            r = await c.get("/stats", headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert stats.call_args.kwargs["tenant_id"] == TENANT


async def test_stats_for_a_tokenless_header_tenant_are_refused(monkeypatch):
    _multi(monkeypatch)
    with patch("robothor.memory.facts.get_memory_stats") as stats:
        async with _client() as c:
            r = await c.get("/stats", headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 401
    stats.assert_not_called()


async def test_stats_without_a_tenant_are_refused_when_several_exist(monkeypatch):
    _multi(monkeypatch)
    with patch("robothor.memory.facts.get_memory_stats") as stats:
        async with _client() as c:
            r = await c.get("/stats")
    assert r.status_code == 400
    stats.assert_not_called()


# ─── /query and /v1/chat/completions ────────────────────────────────


def _pipeline_stubs():
    search = MagicMock(return_value=[])
    return search, [
        patch("robothor.memory.facts.search_facts_compat", new=search),
        patch("robothor.rag.pipeline.search_web", new=AsyncMock(return_value=[])),
        patch("robothor.llm.ollama.generate", new=AsyncMock(return_value="ok")),
        patch("robothor.llm.ollama.chat", new=AsyncMock(return_value="ok")),
    ]


QUERY_ENDPOINTS = [
    ("/query", {"question": "what does Alice drink"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "what does Alice drink"}]}),
]


async def _post_with_stubs(path, body, headers):
    search, stubs = _pipeline_stubs()
    for s in stubs:
        s.start()
    try:
        async with _client() as c:
            r = await c.post(path, json=body, headers=headers)
    finally:
        for s in stubs:
            s.stop()
    return r, search


@pytest.mark.parametrize(("path", "body"), QUERY_ENDPOINTS)
async def test_query_endpoints_search_the_token_tenant(monkeypatch, path, body):
    _multi(monkeypatch)
    r, search = await _post_with_stubs(path, body, _bearer(TENANT))
    assert r.status_code == 200, r.text
    assert search.call_args.kwargs["tenant_id"] == TENANT


@pytest.mark.parametrize(("path", "body"), QUERY_ENDPOINTS)
async def test_query_endpoints_refuse_a_tokenless_header_tenant(monkeypatch, path, body):
    _multi(monkeypatch)
    r, search = await _post_with_stubs(path, body, {"X-Tenant-Id": TENANT})
    assert r.status_code == 401
    search.assert_not_called()


async def test_query_without_a_tenant_is_refused_when_several_exist(monkeypatch):
    _multi(monkeypatch)
    r, search = await _post_with_stubs("/query", {"question": "anything"}, {})
    assert r.status_code == 400
    search.assert_not_called()


async def test_the_pipeline_searches_the_tenant_it_is_given():
    from robothor.rag.pipeline import run_pipeline

    search, stubs = _pipeline_stubs()
    for s in stubs:
        s.start()
    try:
        await run_pipeline(query="what does Alice drink", tenant_id=TENANT)
    finally:
        for s in stubs:
            s.stop()
    assert search.call_args.kwargs["tenant_id"] == TENANT


# ─── Integration ────────────────────────────────────────────────────


@pytest.fixture
def ingest_fixture():
    """A fresh tenant, a unique fact text, and the LLM steps of ingestion stubbed."""
    from robothor.db.connection import get_connection

    tenant = f"test-tenant-{uuid4().hex[:10]}"
    text = f"Bob prefers coffee {uuid4().hex[:6]}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tenant, tenant))
        conn.commit()
    from robothor.memory.facts import Extraction

    fact = {"fact_text": text, "category": "preference", "entities": ["Bob"], "confidence": 0.9}
    # Ingestion runs through the one write path (robothor/memory/write_path.py):
    # one extraction call, then conflict handling, then the fact and its graph.
    stubs = [
        patch(
            "robothor.memory.facts.extract_facts_detailed",
            new=AsyncMock(return_value=Extraction(facts=[fact])),
        ),
        patch(
            "robothor.memory.write_path.resolve_and_store",
            new=AsyncMock(side_effect=RuntimeError("synthetic: no conflict model")),
        ),
        patch("robothor.llm.ollama.get_embedding_async", new=AsyncMock(return_value=[0.0] * 1024)),
        patch(
            "robothor.llm.ollama.get_embeddings_batch_async",
            new=AsyncMock(side_effect=lambda texts, *_a, **_k: [[0.0] * 1024 for _ in texts]),
        ),
    ]
    for s in stubs:
        s.start()
    try:
        yield tenant, text
    finally:
        for s in stubs:
            s.stop()
        with get_connection() as conn:
            cur = conn.cursor()
            # By the unique text, in every tenant: a fact that went to the wrong
            # tenant must not survive the test that caught it.
            cur.execute("DELETE FROM memory_facts WHERE fact_text = %s", (text,))
            cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))
            conn.commit()


def _fact_tenants(text):
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM memory_facts WHERE fact_text = %s", (text,))
        return {row[0] for row in cur.fetchall()}


@pytest.mark.integration
async def test_an_ingested_fact_is_stored_under_the_token_tenant(ingest_fixture):
    tenant, text = ingest_fixture
    async with _client() as c:
        r = await c.post(
            "/ingest", json={"content": text, "source_channel": "api"}, headers=_bearer(tenant)
        )
    assert r.status_code == 200, r.text
    assert _fact_tenants(text) == {tenant}


@pytest.mark.integration
async def test_a_tokenless_header_writes_nothing_anywhere(ingest_fixture):
    # The real test database holds many active tenants, so this is the
    # multi-tenant case end to end: refused, and no row in any tenant.
    tenant, text = ingest_fixture
    async with _client() as c:
        r = await c.post(
            "/ingest",
            json={"content": text, "source_channel": "api"},
            headers={"X-Tenant-Id": tenant},
        )
    assert r.status_code == 401
    assert _fact_tenants(text) == set()
