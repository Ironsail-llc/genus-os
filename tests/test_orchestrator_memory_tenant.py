"""The orchestrator's memory endpoints act as the caller's tenant.

``/ingest``, ``/stats``, ``/query`` and ``/v1/chat/completions`` used to pass no
tenant, so every caller read and wrote the default tenant's memory. The caller's
tenant now comes from, in order:

1. a verified bearer token (the dashboard forwards the session's bridge token),
   whose tenant an ``X-Tenant-Id`` header may not contradict;
2. the ``X-Tenant-Id`` header -- the service-to-service convention the bridge
   and engine already use;
3. the deployment's only tenant, when it has exactly one.

Anything else is refused, never defaulted.
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


def _tenants(monkeypatch, ids):
    import robothor.crm.dal as dal

    monkeypatch.setattr(
        dal, "list_tenants", lambda parent_id=None, active_only=True: [{"id": i} for i in ids]
    )


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _bearer(tenant):
    from robothor.auth.tokens import issue_access_token

    return {"Authorization": f"Bearer {issue_access_token('alice', tenant, 'admin')}"}


def _ingest():
    return patch(
        "robothor.memory.ingestion.ingest_content",
        new=AsyncMock(return_value={"facts_processed": 0}),
    )


BODY = {"content": "Alice prefers tea", "source_channel": "api"}


# ─── /ingest ────────────────────────────────────────────────────────


async def test_ingest_stores_under_the_header_tenant(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == TENANT


async def test_ingest_stores_under_the_verified_token_tenant(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers=_bearer(TENANT))
    assert r.status_code == 200, r.text
    assert ingest.await_args.kwargs["tenant_id"] == TENANT


async def test_a_header_contradicting_the_token_is_refused(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post(
                "/ingest", json=BODY, headers={**_bearer(TENANT), "X-Tenant-Id": "tenant-b"}
            )
    assert r.status_code == 403
    ingest.assert_not_called()


async def test_an_invalid_token_is_refused(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with _ingest() as ingest:
        async with _client() as c:
            r = await c.post("/ingest", json=BODY, headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401
    ingest.assert_not_called()


async def test_no_tenant_in_a_multi_tenant_deployment_is_refused(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
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


async def test_stats_count_the_callers_tenant(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with patch("robothor.memory.facts.get_memory_stats", return_value={"total": 0}) as stats:
        async with _client() as c:
            r = await c.get("/stats", headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 200, r.text
    assert stats.call_args.kwargs["tenant_id"] == TENANT


async def test_stats_without_a_tenant_are_refused_when_several_exist(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
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


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/query", {"question": "what does Alice drink"}),
        (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "what does Alice drink"}]},
        ),
    ],
)
async def test_query_endpoints_search_the_callers_tenant(monkeypatch, path, body):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    search, stubs = _pipeline_stubs()
    for s in stubs:
        s.start()
    try:
        async with _client() as c:
            r = await c.post(path, json=body, headers={"X-Tenant-Id": TENANT})
    finally:
        for s in stubs:
            s.stop()
    assert r.status_code == 200, r.text
    assert search.call_args.kwargs["tenant_id"] == TENANT


async def test_query_without_a_tenant_is_refused_when_several_exist(monkeypatch):
    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    search, stubs = _pipeline_stubs()
    for s in stubs:
        s.start()
    try:
        async with _client() as c:
            r = await c.post("/query", json={"question": "anything"})
    finally:
        for s in stubs:
            s.stop()
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


@pytest.mark.integration
async def test_an_ingested_fact_is_stored_under_the_callers_tenant():
    from robothor.db.connection import get_connection

    tenant = f"test-tenant-{uuid4().hex[:10]}"
    text = f"Bob prefers coffee {uuid4().hex[:6]}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tenant, tenant))
        conn.commit()
    fact = {"fact_text": text, "category": "preference", "entities": ["Bob"], "confidence": 0.9}
    try:
        with (
            patch("robothor.memory.ingestion.extract_facts", new=AsyncMock(return_value=[fact])),
            patch(
                "robothor.memory.ingestion.resolve_and_store",
                new=AsyncMock(side_effect=RuntimeError("synthetic: no conflict model")),
            ),
            patch(
                "robothor.memory.ingestion.extract_entities_batch",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "robothor.llm.ollama.get_embedding_async", new=AsyncMock(return_value=[0.0] * 1024)
            ),
        ):
            async with _client() as c:
                r = await c.post(
                    "/ingest",
                    json={"content": text, "source_channel": "api"},
                    headers={"X-Tenant-Id": tenant},
                )
        assert r.status_code == 200, r.text
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tenant_id FROM memory_facts WHERE fact_text = %s", (text,))
            assert {row[0] for row in cur.fetchall()} == {tenant}
    finally:
        with get_connection() as conn:
            cur = conn.cursor()
            # By the unique text, in every tenant: a fact that went to the wrong
            # tenant must not survive the test that caught it.
            cur.execute("DELETE FROM memory_facts WHERE fact_text = %s", (text,))
            cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))
            conn.commit()
