"""The knowledge-base tables on PostgreSQL, and PgKnowledgeBackend against them.

The unit tests prove the upsert/search rules on the in-process backend; these
prove the real tables keep the same promises: sha-skip, chunk replacement,
hybrid search, collection and tenant isolation, and the tenant policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from robothor.knowledge import search as kb_search
from robothor.knowledge import store

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "crm" / "migrations"
MANIFEST = Path(__file__).resolve().parents[2] / "migrations" / "manifest.txt"
MIGRATION = "155_knowledge_base"
LONG = " ".join(f"word{i}" for i in range(1000))


def _run(coro):
    return asyncio.run(coro)


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    assert f"crm/{MIGRATION}.sql" in MANIFEST.read_text().splitlines()


def test_migration_matches_the_memory_embedding_width():
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    assert "vector(1024)" in sql
    assert "hnsw (embedding vector_cosine_ops)" in sql
    assert "USING GIN (tsv)" in sql


@pytest.fixture
def pg(scratch_db):
    import psycopg2

    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    return db, store.PgKnowledgeBackend(connect)


def _upsert(
    backend,
    embed,
    *,
    tenant="test-tenant",
    collection="helpdesk/policies",
    doc_id="refunds",
    title="Refunds",
    text="Refunds are issued within 14 days.",
):
    return _run(
        store.upsert_document(
            tenant,
            collection,
            doc_id,
            title,
            text,
            source_uri="https://example.com/r",
            metadata={"lang": "en"},
            embed=embed,
            backend=backend,
        )
    )


@pytest.mark.integration
def test_upsert_round_trip_with_sha_skip_and_replacement(pg, embed):
    db, backend = pg
    created = _upsert(backend, embed, text=LONG)
    assert created["status"] == "created"
    assert created["chunks"] > 1
    assert _upsert(backend, embed, text=LONG) == {
        "status": "unchanged",
        "version": 1,
        "chunks": created["chunks"],
    }
    result = _upsert(backend, embed, text="Refunds within 30 days.")
    assert result == {"status": "updated", "version": 2, "chunks": 1}

    with db.cursor() as cur:
        cur.execute("SELECT chunk_ix, text FROM kb_chunks WHERE doc_id = 'refunds'")
        assert cur.fetchall() == [(0, "Refunds within 30 days.")]
        cur.execute("SELECT version, metadata FROM kb_documents WHERE doc_id = 'refunds'")
        assert cur.fetchone() == (2, {"lang": "en"})


@pytest.mark.integration
def test_hybrid_search_is_scoped_to_collection_and_tenant(pg, embed):
    _db, backend = pg
    _upsert(backend, embed, doc_id="refunds")
    _upsert(
        backend,
        embed,
        doc_id="shipping",
        title="Shipping",
        text="Orders ship in two business days by ground courier.",
    )
    _upsert(
        backend,
        embed,
        collection="sales/playbooks",
        doc_id="pitch",
        text="Refunds are a selling point.",
    )
    _upsert(backend, embed, tenant="tenant-b", doc_id="other", text="Refunds for the other tenant.")

    results = _run(
        kb_search.search(
            "test-tenant", "helpdesk/policies", "refunds issued", embed=embed, backend=backend
        )
    )
    assert results[0]["doc_id"] == "refunds"
    assert results[0]["score"] == pytest.approx(2 / 60, rel=1e-4)
    assert results[0]["source_uri"] == "https://example.com/r"
    assert {r["doc_id"] for r in results} <= {"refunds", "shipping"}


@pytest.mark.integration
def test_delete_cascades_and_list_counts(pg, embed):
    db, backend = pg
    _upsert(backend, embed, doc_id="a", text=LONG)
    _upsert(backend, embed, doc_id="b")
    listed = _run(store.list_collections("test-tenant", backend=backend))
    assert listed[0]["collection"] == "helpdesk/policies"
    assert listed[0]["documents"] == 2

    assert (
        _run(store.delete_document("test-tenant", "helpdesk/policies", "a", backend=backend))
        is True
    )
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM kb_chunks WHERE doc_id = 'a'")
        assert cur.fetchone() == (0,)
    assert _run(store.list_collections("tenant-b", backend=backend)) == []


@pytest.mark.integration
def test_both_tables_carry_the_tenant_policy(pg):
    db, _backend = pg
    with db.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('kb_documents', 'kb_chunks') ORDER BY relname"
        )
        assert cur.fetchall() == [("kb_chunks", True, True), ("kb_documents", True, True)]
        cur.execute(
            "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation' "
            "AND tablename IN ('kb_documents', 'kb_chunks') ORDER BY tablename"
        )
        assert cur.fetchall() == [("kb_chunks",), ("kb_documents",)]


@pytest.mark.integration
def test_a_scoped_non_superuser_sees_only_its_own_tenant(pg, scratch_db, embed):
    """The policy itself, enforced by PostgreSQL for a role that cannot bypass it.

    The scratch connection is a superuser, which bypasses RLS, so the check
    runs as a temporary NOSUPERUSER NOBYPASSRLS role bound to one tenant.
    """
    from uuid import uuid4

    import psycopg2
    from psycopg2 import sql

    _db, backend = pg
    _db, dsn = scratch_db(through=MIGRATION)  # already migrated: the same database
    _upsert(backend, embed, tenant="test-tenant", doc_id="mine")
    _upsert(backend, embed, tenant="tenant-b", doc_id="theirs")

    conn = psycopg2.connect(dsn)
    role = "kb_rls_" + uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE ROLE {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role))
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT ON kb_documents, kb_chunks TO {}").format(
                    sql.Identifier(role)
                )
            )
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            cur.execute("SELECT set_config('app.tenant_id', 'test-tenant', true)")
            for table in ("kb_documents", "kb_chunks"):
                cur.execute(
                    sql.SQL("SELECT DISTINCT tenant_id FROM {}").format(sql.Identifier(table))
                )
                assert cur.fetchall() == [("test-tenant",)], table
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO kb_documents (tenant_id, collection, doc_id, sha256) "
                    "VALUES ('tenant-b', 'helpdesk/policies', 'forged', 'x')"
                )
    finally:
        conn.rollback()  # Drops the temporary role with the transaction.
        conn.close()
