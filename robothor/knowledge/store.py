"""Knowledge-base documents: upsert, delete, list (migration 155).

A *collection* is a named set of curated reference documents, named
``<plugin>/<topic>`` (``helpdesk/policies``). A document is identified by
``(tenant_id, collection, doc_id)``; its text is split into overlapping chunks
(:mod:`robothor.knowledge.chunking`) and every chunk is embedded with the model
memory uses, so :mod:`robothor.knowledge.search` can fuse a vector leg and a
keyword leg.

Upsert rules, whichever backend holds the rows:

* the document's sha256 covers its title, text, source_uri and metadata;
* a matching hash is ``unchanged`` -- nothing is embedded or written;
* otherwise every chunk is replaced and ``version`` goes up by one
  (``created`` starts at 1).

:class:`PgKnowledgeBackend` is the real store; :class:`MemoryKnowledgeBackend`
is the in-process one the unit tests use. Backends are synchronous and run off
the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from robothor.constants import DEFAULT_TENANT
from robothor.knowledge.chunking import chunk_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

__all__ = [
    "KnowledgeBackend",
    "KnowledgeWriteConflictError",
    "MemoryKnowledgeBackend",
    "PgKnowledgeBackend",
    "default_embed",
    "delete_document",
    "get_backend",
    "list_collections",
    "resolve_tenant",
    "upsert_document",
    "validate_collection",
]

_SEGMENT = r"[a-z0-9][a-z0-9_.-]*"
_COLLECTION = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})*$")
MAX_COLLECTION_LEN = 128
MAX_DOC_ID_LEN = 256


def validate_collection(collection: str) -> str:
    """``<plugin>/<topic>``: lowercase segments of ``a-z 0-9 _ . -``, joined by ``/``."""
    if not collection or len(collection) > MAX_COLLECTION_LEN or not _COLLECTION.match(collection):
        raise ValueError(
            "collection must look like <plugin>/<topic>: lowercase letters, digits, "
            f"'_', '.', '-', segments joined by '/', at most {MAX_COLLECTION_LEN} characters"
        )
    return collection


def _validate_doc_id(doc_id: str) -> str:
    if not doc_id or not doc_id.strip() or len(doc_id) > MAX_DOC_ID_LEN:
        raise ValueError(f"doc_id is required (at most {MAX_DOC_ID_LEN} characters)")
    return doc_id


def resolve_tenant(tenant_id: str) -> str:
    """An empty tenant means the instance default."""
    return tenant_id or DEFAULT_TENANT


async def default_embed(text: str) -> list[float]:
    """The embedding path memory uses (same model, same width as memory_facts)."""
    from robothor.llm import ollama as llm_client

    return await llm_client.get_embedding_async(text)


def document_sha256(title: str, text: str, source_uri: str, metadata: dict[str, Any]) -> str:
    payload = json.dumps(
        {"title": title, "text": text, "source_uri": source_uri, "metadata": metadata},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentWrite:
    """Everything one upsert writes: the document row and its replacement chunks."""

    tenant_id: str
    collection: str
    doc_id: str
    title: str
    source_uri: str
    sha256: str
    metadata: dict[str, Any]
    chunks: list[tuple[str, list[float]]]


class KnowledgeWriteConflictError(RuntimeError):
    """A concurrent delete removed the document mid-upsert; the caller may retry."""


class KnowledgeBackend(Protocol):
    def current(self, tenant_id: str, collection: str, doc_id: str) -> dict[str, Any] | None:
        """``{"version", "sha256", "chunks"}`` for a stored document, or None."""
        ...

    def write(self, doc: DocumentWrite) -> tuple[str, int]:
        """Upsert unless the stored hash already matches; ``(status, version)``."""
        ...

    def delete(self, tenant_id: str, collection: str, doc_id: str) -> bool: ...

    def collections(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def vector_search(
        self, tenant_id: str, collection: str, embedding: list[float], limit: int
    ) -> list[dict[str, Any]]:
        """Chunks nearest by cosine, best first."""
        ...

    def keyword_search(
        self, tenant_id: str, collection: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Chunks matching every query term, best BM25 rank first."""
        ...


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_HIT_COLUMNS = "c.doc_id, c.chunk_ix, c.text, d.title, d.source_uri"
_HIT_FROM = "kb_chunks c JOIN kb_documents d USING (tenant_id, collection, doc_id)"


class PgKnowledgeBackend:
    """kb_documents / kb_chunks. ``connect`` yields a psycopg2 connection;
    the default borrows a pooled one bound to the tenant for RLS."""

    def __init__(self, connect: Callable[[], contextlib.AbstractContextManager[Any]] | None = None):
        self._connect = connect

    @contextlib.contextmanager
    def _conn(self, tenant_id: str) -> Iterator[Any]:
        if self._connect is not None:
            with self._connect() as conn:
                yield conn
            return
        from robothor.db.connection import get_connection, tenant_scope

        with tenant_scope(tenant_id), get_connection() as conn:
            yield conn

    def current(self, tenant_id: str, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._conn(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.version, d.sha256,
                       (SELECT count(*) FROM kb_chunks c
                         WHERE c.tenant_id = d.tenant_id AND c.collection = d.collection
                           AND c.doc_id = d.doc_id)
                FROM kb_documents d
                WHERE d.tenant_id = %s AND d.collection = %s AND d.doc_id = %s
                """,
                (tenant_id, collection, doc_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"version": row[0], "sha256": row[1], "chunks": row[2]}

    def write(self, doc: DocumentWrite) -> tuple[str, int]:
        from psycopg2.extras import Json, execute_values

        key = (doc.tenant_id, doc.collection, doc.doc_id)
        with self._conn(doc.tenant_id) as conn:
            with conn.cursor() as cur:
                # The hash check is repeated here, under the row lock the upsert
                # takes, so two identical upserts racing cannot both bump version.
                cur.execute(
                    """
                    INSERT INTO kb_documents
                        (tenant_id, collection, doc_id, title, source_uri, version, sha256,
                         metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 1, %s, %s, now())
                    ON CONFLICT (tenant_id, collection, doc_id) DO UPDATE
                       SET title = EXCLUDED.title, source_uri = EXCLUDED.source_uri,
                           sha256 = EXCLUDED.sha256, metadata = EXCLUDED.metadata,
                           version = kb_documents.version + 1, updated_at = now()
                     WHERE kb_documents.sha256 IS DISTINCT FROM EXCLUDED.sha256
                    RETURNING version, (xmax = 0) AS inserted
                    """,
                    (*key, doc.title, doc.source_uri, doc.sha256, Json(doc.metadata)),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT version FROM kb_documents "
                        "WHERE tenant_id = %s AND collection = %s AND doc_id = %s",
                        key,
                    )
                    unchanged = cur.fetchone()
                    conn.commit()
                    if unchanged is None:
                        raise KnowledgeWriteConflictError(
                            f"{doc.collection}/{doc.doc_id} was deleted while it was being "
                            "written; retry the upsert"
                        )
                    return "unchanged", int(unchanged[0])
                version, inserted = int(row[0]), bool(row[1])
                cur.execute(
                    "DELETE FROM kb_chunks WHERE tenant_id = %s AND collection = %s AND doc_id = %s",
                    key,
                )
                execute_values(
                    cur,
                    "INSERT INTO kb_chunks (tenant_id, collection, doc_id, chunk_ix, text, embedding)"
                    " VALUES %s",
                    [(*key, ix, text, emb) for ix, (text, emb) in enumerate(doc.chunks)],
                    template="(%s, %s, %s, %s, %s, %s::vector)",
                )
            conn.commit()
        return ("created" if inserted else "updated"), version

    def delete(self, tenant_id: str, collection: str, doc_id: str) -> bool:
        with self._conn(tenant_id) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM kb_documents "
                    "WHERE tenant_id = %s AND collection = %s AND doc_id = %s",
                    (tenant_id, collection, doc_id),
                )
                deleted = bool(cur.rowcount)
            conn.commit()
        return deleted

    def collections(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._conn(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.collection, count(*) AS documents,
                       COALESCE(sum(cc.n), 0) AS chunks, max(d.updated_at) AS updated_at
                FROM kb_documents d
                LEFT JOIN (SELECT collection, doc_id, count(*) AS n FROM kb_chunks
                            WHERE tenant_id = %s GROUP BY collection, doc_id) cc
                       USING (collection, doc_id)
                WHERE d.tenant_id = %s
                GROUP BY d.collection
                ORDER BY d.collection
                """,
                (tenant_id, tenant_id),
            )
            rows = cur.fetchall()
        return [
            {"collection": r[0], "documents": int(r[1]), "chunks": int(r[2]), "updated_at": r[3]}
            for r in rows
        ]

    def vector_search(
        self, tenant_id: str, collection: str, embedding: list[float], limit: int
    ) -> list[dict[str, Any]]:
        from psycopg2.extras import RealDictCursor

        from robothor.memory.vector_tuning import apply_hnsw_session

        with self._conn(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            apply_hnsw_session(cur)
            cur.execute(
                f"""
                SELECT {_HIT_COLUMNS}, 1 - (c.embedding <=> %s::vector) AS similarity
                FROM {_HIT_FROM}
                WHERE c.tenant_id = %s AND c.collection = %s AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding, tenant_id, collection, embedding, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def keyword_search(
        self, tenant_id: str, collection: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        from psycopg2.extras import RealDictCursor

        with self._conn(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {_HIT_COLUMNS},
                       ts_rank(c.tsv, plainto_tsquery('english', %s)) AS bm25_score
                FROM {_HIT_FROM}
                WHERE c.tsv @@ plainto_tsquery('english', %s)
                  AND c.tenant_id = %s AND c.collection = %s
                ORDER BY bm25_score DESC, c.doc_id, c.chunk_ix
                LIMIT %s
                """,
                (query, query, tenant_id, collection, limit),
            )
            return [dict(r) for r in cur.fetchall()]


# ── In-process (tests) ───────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


class MemoryKnowledgeBackend:
    """Dict-backed store with the same promises as :class:`PgKnowledgeBackend`.

    The keyword leg is an AND over query terms (as ``plainto_tsquery`` is),
    without stemming; the vector leg is exact cosine.
    """

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._chunks: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def chunks(self, tenant_id: str, collection: str, doc_id: str) -> list[dict[str, Any]]:
        return list(self._chunks.get((tenant_id, collection, doc_id), []))

    def current(self, tenant_id: str, collection: str, doc_id: str) -> dict[str, Any] | None:
        key = (tenant_id, collection, doc_id)
        doc = self._docs.get(key)
        if doc is None:
            return None
        return {
            "version": doc["version"],
            "sha256": doc["sha256"],
            "chunks": len(self._chunks[key]),
        }

    def write(self, doc: DocumentWrite) -> tuple[str, int]:
        key = (doc.tenant_id, doc.collection, doc.doc_id)
        existing = self._docs.get(key)
        if existing is not None and existing["sha256"] == doc.sha256:
            return "unchanged", int(existing["version"])
        version = 1 if existing is None else int(existing["version"]) + 1
        self._docs[key] = {
            "title": doc.title,
            "source_uri": doc.source_uri,
            "sha256": doc.sha256,
            "metadata": dict(doc.metadata),
            "version": version,
            "updated_at": datetime.now(UTC),
        }
        self._chunks[key] = [
            {"chunk_ix": ix, "text": text, "embedding": emb}
            for ix, (text, emb) in enumerate(doc.chunks)
        ]
        return ("created" if existing is None else "updated"), version

    def delete(self, tenant_id: str, collection: str, doc_id: str) -> bool:
        key = (tenant_id, collection, doc_id)
        self._chunks.pop(key, None)
        return self._docs.pop(key, None) is not None

    def collections(self, tenant_id: str) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for (tenant, collection, _doc_id), doc in sorted(self._docs.items()):
            if tenant != tenant_id:
                continue
            row = out.setdefault(
                collection,
                {"collection": collection, "documents": 0, "chunks": 0, "updated_at": None},
            )
            row["documents"] += 1
            row["chunks"] += len(self._chunks[(tenant, collection, _doc_id)])
            if row["updated_at"] is None or doc["updated_at"] > row["updated_at"]:
                row["updated_at"] = doc["updated_at"]
        return [out[name] for name in sorted(out)]

    def _hits(self, tenant_id: str, collection: str) -> Iterator[dict[str, Any]]:
        for (tenant, coll, doc_id), chunks in sorted(self._chunks.items()):
            if tenant != tenant_id or coll != collection:
                continue
            doc = self._docs[(tenant, coll, doc_id)]
            for c in chunks:
                yield {
                    "doc_id": doc_id,
                    "chunk_ix": c["chunk_ix"],
                    "text": c["text"],
                    "title": doc["title"],
                    "source_uri": doc["source_uri"],
                    "embedding": c["embedding"],
                }

    def vector_search(
        self, tenant_id: str, collection: str, embedding: list[float], limit: int
    ) -> list[dict[str, Any]]:
        def cosine(other: list[float]) -> float:
            dot = sum(a * b for a, b in zip(embedding, other, strict=False))
            norm = math.sqrt(sum(a * a for a in embedding)) * math.sqrt(sum(b * b for b in other))
            return dot / norm if norm else 0.0

        hits = []
        for hit in self._hits(tenant_id, collection):
            emb = hit.pop("embedding")
            if emb:
                hit["similarity"] = cosine(emb)
                hits.append(hit)
        hits.sort(key=lambda h: -h["similarity"])
        return hits[:limit]

    def keyword_search(
        self, tenant_id: str, collection: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        terms = _terms(query)
        if not terms:
            return []
        hits = []
        for hit in self._hits(tenant_id, collection):
            hit.pop("embedding")
            words = _terms(hit["text"])
            if all(t in words for t in terms):
                hit["bm25_score"] = float(sum(words.count(t) for t in terms)) / len(words)
                hits.append(hit)
        hits.sort(key=lambda h: -h["bm25_score"])
        return hits[:limit]


# ── API ──────────────────────────────────────────────────────────────────────

_default_backend: KnowledgeBackend | None = None


def get_backend() -> KnowledgeBackend:
    """The process-wide PostgreSQL backend."""
    global _default_backend
    if _default_backend is None:
        _default_backend = PgKnowledgeBackend()
    return _default_backend


async def upsert_document(
    tenant_id: str,
    collection: str,
    doc_id: str,
    title: str,
    text: str,
    source_uri: str = "",
    metadata: dict[str, Any] | None = None,
    *,
    embed: Callable[[str], Awaitable[list[float]]] | None = None,
    backend: KnowledgeBackend | None = None,
) -> dict[str, Any]:
    """Store a document; ``{"status": "unchanged"|"created"|"updated", "version", "chunks"}``.

    Raises ValueError for a malformed collection, a missing doc_id or blank
    text. An embedding failure propagates: a document is never stored half-indexed.
    """
    tenant = resolve_tenant(tenant_id)
    validate_collection(collection)
    _validate_doc_id(doc_id)
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("text is empty")
    meta = dict(metadata or {})
    store = backend or get_backend()
    sha = document_sha256(title, text, source_uri, meta)

    current = await asyncio.to_thread(store.current, tenant, collection, doc_id)
    if current is not None and current["sha256"] == sha:
        return {"status": "unchanged", "version": current["version"], "chunks": current["chunks"]}

    embedder = embed or default_embed
    embedded = [(chunk, await embedder(chunk)) for chunk in chunks]
    status, version = await asyncio.to_thread(
        store.write,
        DocumentWrite(tenant, collection, doc_id, title, source_uri, sha, meta, embedded),
    )
    return {"status": status, "version": version, "chunks": len(chunks)}


async def delete_document(
    tenant_id: str, collection: str, doc_id: str, *, backend: KnowledgeBackend | None = None
) -> bool:
    """Remove a document and its chunks; False when there was none."""
    validate_collection(collection)
    _validate_doc_id(doc_id)
    store = backend or get_backend()
    return await asyncio.to_thread(store.delete, resolve_tenant(tenant_id), collection, doc_id)


async def list_collections(
    tenant_id: str, *, backend: KnowledgeBackend | None = None
) -> list[dict[str, Any]]:
    """``[{"collection", "documents", "chunks", "updated_at"}]`` for one tenant."""
    store = backend or get_backend()
    return await asyncio.to_thread(store.collections, resolve_tenant(tenant_id))
