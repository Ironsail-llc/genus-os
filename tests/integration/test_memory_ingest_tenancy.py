"""Ingested facts land in the caller's tenant, not the default one.

``ingest_content`` is the shared entry point for every channel that feeds the
memory system — email, telegram, gchat, voice, camera, crm, and the
conversation-session ingest that runs after each interactive agent run. It had
no ``tenant_id`` parameter at all, so every fact it stored fell through to
``DEFAULT_TENANT`` regardless of which tenant produced the content.

``ingest_conversation_session`` makes the gap visible: it accepts ``tenant_id``,
threads it into the ingest watermarks, and then calls ``ingest_content``
without it — so the bookkeeping is tenant-correct while the facts themselves
are not.

Every downstream function (``resolve_and_store``, ``store_fact``,
``extract_entities_batch``, ``populate_vault_from_content``) already takes a
tenant. Only the caller dropped it.

On a single-tenant instance this is invisible, because DEFAULT_TENANT happens
to be the only tenant. It becomes a cross-tenant write the moment a second
tenant ingests anything.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_CONSTANT_VECTOR = [0.1] * 1024


@pytest.fixture
def _stub_llm(monkeypatch):
    """Stub only the LLM boundary: extraction and embeddings.

    Both are external services, not the control under test. The control is
    which tenant the row is written to, and no amount of stubbing here can
    make a wrong tenant look right — the assertion reads the database.
    """
    from robothor.memory import ingestion

    async def _fake_extract(_content: str, **_kw):
        return [
            {
                "fact_text": "the northern depot runs on a fortnightly schedule",
                "category": "project",
                "entities": [],
                "confidence": 0.9,
            }
        ]

    async def _fake_embedding(_text: str):
        return list(_CONSTANT_VECTOR)

    async def _fake_batch(texts):
        return [list(_CONSTANT_VECTOR) for _ in texts]

    async def _fake_generate(*_a, **_kw):
        # Entity extraction and conflict classification both go through
        # generate(). Returning an empty entity set keeps the ingest path
        # short; the tenant assertion does not depend on what comes back.
        return '{"entities": [], "relations": []}'

    from robothor.llm import ollama as llm_client

    monkeypatch.setattr(ingestion, "extract_facts", _fake_extract)
    monkeypatch.setattr(llm_client, "get_embedding_async", _fake_embedding)
    monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _fake_batch)
    monkeypatch.setattr(llm_client, "generate", _fake_generate)


@pytest.fixture
def ingest_tenant(db_cursor, test_prefix):
    tenant = f"{test_prefix}-tenant"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    return tenant


@pytest.mark.asyncio
async def test_ingest_content_writes_to_the_given_tenant(
    ingest_tenant, db_cursor, mock_get_connection, _stub_llm
):
    """A fact ingested for tenant X must be stored under tenant X."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.ingestion import ingest_content

    result = await ingest_content(
        content="The northern depot runs on a fortnightly schedule this quarter.",
        source_channel="email",
        content_type="conversation",
        tenant_id=ingest_tenant,
    )

    fact_ids = result.get("fact_ids") or []
    assert fact_ids, f"nothing was stored: {result}"

    db_cursor.execute(
        "SELECT id, tenant_id FROM memory_facts WHERE id = ANY(%s)",
        (fact_ids,),
    )
    rows = db_cursor.fetchall()
    assert rows, "stored ids do not resolve to rows"
    landed = {r["tenant_id"] for r in rows}
    assert landed == {ingest_tenant}, (
        f"ingested facts landed in {landed} instead of {ingest_tenant!r}; "
        f"DEFAULT_TENANT is {DEFAULT_TENANT!r}"
    )


@pytest.mark.asyncio
async def test_ingest_content_still_defaults_when_no_tenant_given(
    db_cursor, mock_get_connection, _stub_llm
):
    """Omitting the tenant keeps the historical behaviour.

    The parameter is additive; existing callers that never passed a tenant must
    not change behaviour when this ships.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.ingestion import ingest_content

    result = await ingest_content(
        content="An unattributed note about depot scheduling policy.",
        source_channel="email",
        content_type="conversation",
    )
    fact_ids = result.get("fact_ids") or []
    assert fact_ids

    db_cursor.execute("SELECT tenant_id FROM memory_facts WHERE id = ANY(%s)", (fact_ids,))
    assert {r["tenant_id"] for r in db_cursor.fetchall()} == {DEFAULT_TENANT}


def test_conversation_ingest_forwards_its_tenant():
    """Pin the call site.

    ingest_conversation_session already accepts tenant_id and uses it for the
    watermark bookkeeping. Adding the parameter to ingest_content while leaving
    this call site unchanged would fix the signature and none of the behaviour.
    """
    import inspect

    from robothor.memory import conversation_ingest

    src = inspect.getsource(conversation_ingest.ingest_conversation_session)
    call = src[src.index("ingest_content(") :]
    call = call[: call.index(")\n")]
    assert "tenant_id" in call, (
        "ingest_conversation_session calls ingest_content without a tenant; "
        "conversation facts would be written to DEFAULT_TENANT"
    )
