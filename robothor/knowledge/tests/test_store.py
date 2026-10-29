"""upsert / delete / list against the in-process backend.

The version and sha-skip rules live in ``robothor.knowledge.store`` and are the
same whichever backend holds the rows; ``test_pg_store.py`` proves PostgreSQL
keeps the same promises.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.knowledge import store
from robothor.knowledge.store import MemoryKnowledgeBackend

LONG = " ".join(f"word{i}" for i in range(1000))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def backend() -> MemoryKnowledgeBackend:
    return MemoryKnowledgeBackend()


def _upsert(
    backend,
    embed,
    *,
    tenant="test-tenant",
    collection="helpdesk/policies",
    doc_id="refunds",
    title="Refunds",
    text="Refunds within 14 days.",
    **kw,
):
    return _run(
        store.upsert_document(
            tenant, collection, doc_id, title, text, embed=embed, backend=backend, **kw
        )
    )


def test_first_upsert_creates_version_one(backend, embed):
    result = _upsert(backend, embed)
    assert result == {"status": "created", "version": 1, "chunks": 1}


def test_same_content_is_unchanged_and_not_re_embedded(backend, embed):
    _upsert(backend, embed)
    calls = len(embed.calls)
    result = _upsert(backend, embed)
    assert result == {"status": "unchanged", "version": 1, "chunks": 1}
    assert len(embed.calls) == calls


def test_changed_content_bumps_version_and_replaces_chunks(backend, embed):
    _upsert(backend, embed, text=LONG)
    assert len(backend.chunks("test-tenant", "helpdesk/policies", "refunds")) > 1

    result = _upsert(backend, embed, text="Refunds within 30 days.")
    assert result == {"status": "updated", "version": 2, "chunks": 1}
    texts = [c["text"] for c in backend.chunks("test-tenant", "helpdesk/policies", "refunds")]
    assert texts == ["Refunds within 30 days."]


def test_a_title_or_source_change_is_an_update(backend, embed):
    _upsert(backend, embed)
    assert _upsert(backend, embed, title="Refund policy")["status"] == "updated"
    result = _upsert(backend, embed, title="Refund policy", source_uri="https://example.com/r")
    assert result["status"] == "updated"
    assert result["version"] == 3


def test_every_chunk_is_embedded(backend, embed):
    result = _upsert(backend, embed, text=LONG)
    assert len(embed.calls) == result["chunks"]
    assert all(
        c["embedding"] for c in backend.chunks("test-tenant", "helpdesk/policies", "refunds")
    )


def test_empty_text_is_refused(backend, embed):
    with pytest.raises(ValueError):
        _upsert(backend, embed, text="   ")


@pytest.mark.parametrize("bad", ["", "Has Spaces", "/leading", "trailing/", "a//b", "x" * 200])
def test_a_malformed_collection_name_is_refused(backend, embed, bad):
    with pytest.raises(ValueError):
        _upsert(backend, embed, collection=bad)


def test_a_missing_doc_id_is_refused(backend, embed):
    with pytest.raises(ValueError):
        _upsert(backend, embed, doc_id="")


def test_delete_removes_the_document_and_its_chunks(backend, embed):
    _upsert(backend, embed, text=LONG)
    assert (
        _run(store.delete_document("test-tenant", "helpdesk/policies", "refunds", backend=backend))
        is True
    )
    assert backend.chunks("test-tenant", "helpdesk/policies", "refunds") == []
    assert (
        _run(store.delete_document("test-tenant", "helpdesk/policies", "refunds", backend=backend))
        is False
    )


def test_deleted_then_re_added_starts_again_at_version_one(backend, embed):
    _upsert(backend, embed)
    _run(store.delete_document("test-tenant", "helpdesk/policies", "refunds", backend=backend))
    assert _upsert(backend, embed)["status"] == "created"


def test_list_collections_counts_documents_and_chunks(backend, embed):
    _upsert(backend, embed, doc_id="a", text=LONG)
    _upsert(backend, embed, doc_id="b")
    _upsert(backend, embed, collection="sales/playbooks", doc_id="c")

    listed = _run(store.list_collections("test-tenant", backend=backend))
    by_name = {c["collection"]: c for c in listed}
    assert [c["collection"] for c in listed] == ["helpdesk/policies", "sales/playbooks"]
    a_chunks = len(backend.chunks("test-tenant", "helpdesk/policies", "a"))
    assert by_name["helpdesk/policies"]["documents"] == 2
    assert by_name["helpdesk/policies"]["chunks"] == a_chunks + 1
    assert by_name["sales/playbooks"] == {
        "collection": "sales/playbooks",
        "documents": 1,
        "chunks": 1,
        "updated_at": by_name["sales/playbooks"]["updated_at"],
    }
    assert by_name["sales/playbooks"]["updated_at"] is not None


def test_tenants_do_not_see_each_others_documents(backend, embed):
    _upsert(backend, embed, tenant="tenant-a")
    assert _run(store.list_collections("tenant-b", backend=backend)) == []
    # Same key in another tenant is a separate document, not an update.
    assert _upsert(backend, embed, tenant="tenant-b")["status"] == "created"
    assert (
        _run(store.delete_document("tenant-c", "helpdesk/policies", "refunds", backend=backend))
        is False
    )


def test_same_doc_id_in_two_collections_is_two_documents(backend, embed):
    _upsert(backend, embed, collection="helpdesk/policies")
    assert _upsert(backend, embed, collection="helpdesk/faq")["status"] == "created"


def test_an_empty_tenant_means_the_default_tenant(backend, embed, monkeypatch):
    monkeypatch.setattr(store, "DEFAULT_TENANT", "test-tenant")
    _upsert(backend, embed, tenant="")
    listed = _run(store.list_collections("test-tenant", backend=backend))
    assert [c["collection"] for c in listed] == ["helpdesk/policies"]


@pytest.mark.parametrize(
    ("collection", "doc_id"), [("Bad Name", "refunds"), ("helpdesk/policies", "")]
)
def test_delete_validates_collection_and_doc_id(backend, collection, doc_id):
    with pytest.raises(ValueError):
        _run(store.delete_document("test-tenant", collection, doc_id, backend=backend))


class _ScriptedCursor:
    """A cursor whose fetchone answers come from a script, in order."""

    def __init__(self, answers):
        self._answers = list(answers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_kw):
        return None

    def fetchone(self):
        return self._answers.pop(0)


class _ScriptedConn:
    def __init__(self, answers):
        self._cursor = _ScriptedCursor(answers)

    def cursor(self, *_a, **_kw):
        return self._cursor

    def commit(self):
        return None


def test_pg_write_names_the_race_when_the_document_vanishes():
    import contextlib

    @contextlib.contextmanager
    def connect():
        # Upsert returned no row (hash matched), then the row was gone.
        yield _ScriptedConn([None, None])

    doc = store.DocumentWrite(
        "test-tenant", "helpdesk/policies", "refunds", "Refunds", "", "abc", {}, [("t", [0.1])]
    )
    with pytest.raises(store.KnowledgeWriteConflictError, match="retry"):
        store.PgKnowledgeBackend(connect).write(doc)
